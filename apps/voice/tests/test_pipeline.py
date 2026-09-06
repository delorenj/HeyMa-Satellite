import asyncio
import io
import wave
from unittest.mock import AsyncMock

import pytest
from deepgram.listen.v1.types import ListenV1Metadata, ListenV1Results
from line.llm_agent.provider import StreamChunk
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService

from tonny_voice.pipeline import (
    BufferedAudioFrame,
    FinishInputFrame,
    Reply,
    TranscriptionDoneFrame,
    UtteranceDeepgram,
    VoiceEngine,
    VoiceError,
)


class FakeSTT(FrameProcessor):
    def __init__(self, text="Remember my code is Lima.", *, stall=False, error=False):
        super().__init__()
        self.text = text
        self.stall = stall
        self.error = error
        self.audio = bytearray()
        self.cleaned = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BufferedAudioFrame):
            self.audio.extend(frame.audio)
        elif isinstance(frame, FinishInputFrame):
            if self.stall:
                await asyncio.Event().wait()
            if self.error:
                await self.push_error(error_msg="controlled provider refusal")
                return
            await self.push_frame(InterimTranscriptionFrame("WRONG INTERIM", "pi", ""))
            if self.text:
                await self.push_frame(TranscriptionFrame(self.text, "pi", ""))
            await self.push_frame(TranscriptionDoneFrame())
        await self.push_frame(frame, direction)

    async def cleanup(self):
        self.cleaned = True
        await super().cleanup()


class FakeTTS(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.texts = []
        self.cleaned = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSSpeakFrame):
            self.texts.append(frame.text)
            await self.push_frame(TTSAudioRawFrame(b"\x01\x00" * 2400, 24000, 1))
        await self.push_frame(frame, direction)

    async def cleanup(self):
        self.cleaned = True
        await super().cleanup()


async def test_real_pipecat_lifecycle_and_real_line_sdk_preserve_two_turns(settings, monkeypatch):
    engine = VoiceEngine(settings)
    stts, ttss, messages_seen = [], [], []
    real_build_agent = engine.build_agent

    def build_stt():
        stt = FakeSTT("Remember my code is Lima." if not stts else "What is my code?")
        stts.append(stt)
        return stt

    def build_tts():
        tts = FakeTTS()
        ttss.append(tts)
        return tts

    def build_agent():
        agent = real_build_agent()

        async def chat(messages, *_args, **_kwargs):
            messages_seen.append([(m.role, m.content) for m in messages])
            yield StreamChunk(text="Your code ")
            yield StreamChunk(text="is Lima.")

        monkeypatch.setattr(agent._llm, "chat", chat)
        return agent

    monkeypatch.setattr(engine, "build_stt", build_stt)
    monkeypatch.setattr(engine, "build_tts", build_tts)
    monkeypatch.setattr(engine, "build_agent", build_agent)
    pcm = b"\x11\x00" * 7000
    async with asyncio.timeout(10):
        first = await engine.process(pcm)
        assert list(engine.history) == []  # transport has not sent it yet
        engine.remember(first)
        second = await engine.process(pcm)
        engine.remember(second)
    assert second.text == "Your code is Lima."
    assert first.transcript == "Remember my code is Lima."
    assert messages_seen == [
        [("user", "Remember my code is Lima.")],
        [
            ("user", "Remember my code is Lima."),
            ("assistant", "Your code is Lima."),
            ("user", "What is my code?"),
        ],
    ]
    assert all(stt.audio == pcm and stt.cleaned for stt in stts)
    assert all(tts.texts == ["Your code is Lima."] and tts.cleaned for tts in ttss)
    with wave.open(io.BytesIO(second.wav)) as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (24000, 1, 2)
        assert audio.getnframes() == 2400
    assert engine.evidence == {"stt_turns": 2, "llm_turns": 2, "tts_turns": 2}


@pytest.mark.parametrize("mode", ["stall", "refuse", "silence"])
async def test_stt_failures_cancel_and_cleanup_without_calling_llm(settings, monkeypatch, mode):
    engine = VoiceEngine(settings.model_copy(update={"stt_timeout_seconds": 0.15}))
    stt = FakeSTT(text="", stall=mode == "stall", error=mode == "refuse")
    monkeypatch.setattr(engine, "build_stt", lambda: stt)
    agent = AsyncMock()
    monkeypatch.setattr(engine, "answer", agent)
    with pytest.raises(TimeoutError if mode == "stall" else VoiceError):
        await engine.process(b"\x01\x00" * 100)
    assert stt.cleaned
    agent.assert_not_called()
    assert list(engine.history) == []


async def test_deepgram_waits_for_terminal_metadata_after_final_results(monkeypatch):
    service = UtteranceDeepgram(api_key="test-only", sample_rate=16000)
    emitted = []

    async def push(frame, *args):
        emitted.append(frame)

    async def sdk_handler(_self, result):
        if isinstance(result, ListenV1Results) and result.channel.alternatives[0].transcript:
            await push(TranscriptionFrame("final words", "pi", ""))

    monkeypatch.setattr(service, "push_frame", push)
    monkeypatch.setattr(DeepgramSTTService, "_on_message", sdk_handler)
    result = ListenV1Results.model_construct(
        is_final=True,
        from_finalize=False,
        channel={"alternatives": [{"transcript": "final words"}]},
    )
    await service._on_message(result)
    assert [type(f) for f in emitted] == [TranscriptionFrame]
    result = ListenV1Results.model_construct(
        is_final=True,
        from_finalize=True,
        channel={"alternatives": [{"transcript": ""}]},
    )
    await service._on_message(result)
    assert [type(f) for f in emitted] == [TranscriptionFrame]
    service._stream_closing = True
    await service._on_message(ListenV1Metadata.model_construct(duration=0.0))
    assert [type(f) for f in emitted] == [TranscriptionFrame, TranscriptionDoneFrame]


async def test_deepgram_waits_for_connection_before_accepting_buffered_audio(monkeypatch):
    service = UtteranceDeepgram(api_key="test-only", sample_rate=16000)
    monkeypatch.setattr(DeepgramSTTService, "start", AsyncMock())
    started = asyncio.create_task(service.start(StartFrame()))
    await asyncio.sleep(0)
    assert not started.done()
    service._connection_ready.set()
    await asyncio.wait_for(started, 1)


async def test_deepgram_finalize_follows_every_buffered_sample(settings, monkeypatch):
    """Exercise real Deepgram/Pipecat queues with a yielding websocket sender.

    Sending InputAudioRawFrame directly allowed Finalize to overtake audio on
    Pipecat's separate system/data tasks; a live sentence became just "Plea".
    """
    pcm = b"\x10\x00" * 41610
    sent = bytearray()
    finalized_after = []

    class BufferedDeepgram(UtteranceDeepgram):
        async def _connect(self):
            service = self

            class Connection:
                async def send_media(self, audio):
                    await asyncio.sleep(0.001)  # real network sends yield here
                    sent.extend(audio)

                async def send_close_stream(self, _message):
                    finalized_after.append(len(sent))
                    result = ListenV1Results.model_construct(
                        type="Results",
                        is_final=True,
                        from_finalize=True,
                        start=0.0,
                        duration=len(sent) / 32000,
                        channel={
                            "alternatives": [
                                {
                                    "transcript": "Please say the walking skeleton is alive.",
                                    "languages": None,
                                }
                            ]
                        },
                    )
                    await service._on_message(result)
                    await service._on_message(
                        ListenV1Metadata.model_construct(duration=len(sent) / 32000)
                    )

            self._connection = Connection()
            self._connection_ready.set()

        async def _disconnect(self):
            self._connection = None
            self._connection_ready.clear()

    engine = VoiceEngine(settings)
    monkeypatch.setattr(
        engine, "build_stt", lambda: BufferedDeepgram(api_key="test-only", sample_rate=16000)
    )
    assert await engine.transcribe(pcm) == "Please say the walking skeleton is alive."
    assert sent == pcm
    assert finalized_after == [len(pcm)]


async def test_line_history_is_bounded_and_expired_turns_are_absent(settings, monkeypatch):
    engine = VoiceEngine(settings.model_copy(update={"history_turns": 2}))
    for index in range(4):
        engine.remember(Reply(str(index), str(index), b""))
    assert list(engine.history) == [("2", "2"), ("3", "3")]
    engine.last_turn_at = 0
    real_build = engine.build_agent
    observed = []

    def build():
        agent = real_build()

        async def chat(messages, *_args, **_kwargs):
            observed.extend(m.content for m in messages)
            yield StreamChunk(text="Ready.")

        monkeypatch.setattr(agent._llm, "chat", chat)
        return agent

    monkeypatch.setattr(engine, "build_agent", build)
    assert await engine.answer("Hello") == "Ready."
    assert observed == ["Hello"]
