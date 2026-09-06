"""Bounded utterance processing through Pipecat and a standalone Line agent.

The Pi supplies its turn boundary. STT finalization is ordered after all PCM;
no VAD or wake model is loaded here. Separate finite Pipecat pipelines make the
buffered WAV protocol's STT and TTS lifetimes explicit.
"""

import asyncio
import io
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Any

from deepgram.listen.v1.types import ListenV1CloseStream, ListenV1Metadata
from line.agent import AgentEnv, TurnEnv
from line.events import AgentSendText, AgentTextSent, UserTextSent, UserTurnEnded
from line.llm_agent import LlmAgent, LlmConfig
from pipecat.frames.frames import (
    DataFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.cartesia.tts import CartesiaHttpTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.workers.runner import WorkerRunner

from tonny_voice.config import Settings

PROMPT = (
    "You are Tonny, a friendly household voice assistant speaking through a little "
    "Raspberry Pi. Answer naturally in one or two short sentences. Use plain spoken "
    "words, without markdown. Be honest about uncertainty and your limitations. "
    "You can converse and answer general questions; you have no tools to operate "
    "devices, browse, or perform actions. Never claim to have performed an action."
)


class VoiceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class FinishInputFrame(DataFrame):
    """FIFO barrier: unlike a SystemFrame it cannot overtake queued PCM."""


@dataclass
class BufferedAudioFrame(DataFrame):
    """Keep buffered PCM on the same FIFO as FinishInputFrame.

    InputAudioRawFrame is a SystemFrame in Pipecat 1.7.0. Across processors,
    its priority task can still be sending while a DataFrame arrives on the
    separate ordinary task. Convert to input audio only at the STT call site.
    """

    audio: bytes


@dataclass
class TranscriptionDoneFrame(DataFrame):
    """Deepgram returned terminal stream metadata after draining all audio."""


class UtteranceDeepgram(DeepgramSTTService):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._stream_closing = False
        self._sent_bytes = 0

    async def start(self, frame: StartFrame):
        await super().start(frame)
        # Pipecat starts the connection in a task; buffered input would otherwise
        # be silently dropped by run_stt before the websocket becomes ready.
        await self._connection_ready.wait()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, BufferedAudioFrame):
            await super().process_frame(InputAudioRawFrame(frame.audio, 16000, 1), direction)
        elif isinstance(frame, FinishInputFrame):
            if not self._connection:
                await self.push_error(error_msg="STT connection was lost before end of input")
                return
            self._stream_closing = True
            # Finalize can acknowledge only the first currently processed chunk
            # of a fast upload. CloseStream drains all cached audio and sends
            # terminal Metadata: https://developers.deepgram.com/docs/close-stream
            await self._connection.send_close_stream(ListenV1CloseStream(type="CloseStream"))
        else:
            await super().process_frame(frame, direction)

    async def run_stt(self, audio: bytes):
        if not self._connection:
            await self.push_error(error_msg="STT connection was lost during input")
            return
        async for frame in super().run_stt(audio):
            yield frame
        if self._connection:
            self._sent_bytes += len(audio)
        else:
            await self.push_error(error_msg="STT failed to send an audio frame")

    async def _disconnect(self):
        if self._stream_closing:
            # CloseStream was already sent, and the server may have closed its
            # side. Let the SDK cancel tasks without sending a second close.
            self._connection = None
        await super()._disconnect()

    async def _on_message(self, message):
        await super()._on_message(message)
        if isinstance(message, ListenV1Metadata) and self._stream_closing:
            if abs(message.duration - self._sent_bytes / 32000) > 0.1:
                await self.push_error(
                    error_msg="STT did not acknowledge the complete audio duration"
                )
                return
            # Queue behind all TranscriptionFrames from super(), even if the
            # final result was empty because speech was finalized earlier.
            await self.push_frame(TranscriptionDoneFrame())


class TranscriptCollector(FrameProcessor):
    def __init__(self, limit: int):
        super().__init__()
        self.parts: list[str] = []
        self.limit = limit
        self.finished = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterimTranscriptionFrame):
            return
        if isinstance(frame, TranscriptionFrame):
            if sum(map(len, self.parts)) + len(frame.text) > self.limit:
                await self.push_error(error_msg="transcript exceeds the utterance limit")
                return
            if frame.text.strip():
                self.parts.append(frame.text.strip())
        elif isinstance(frame, TranscriptionDoneFrame):
            self.finished.set()
        await self.push_frame(frame, direction)


class AudioCollector(FrameProcessor):
    def __init__(self, limit: int):
        super().__init__()
        self.audio = bytearray()
        self.limit = limit
        self.sample_rate = 24000

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame):
            if frame.num_channels != 1 or frame.sample_rate != self.sample_rate:
                await self.push_error(error_msg="TTS returned an unexpected audio format")
            elif len(frame.audio) % 2 or len(self.audio) + len(frame.audio) > self.limit:
                await self.push_error(error_msg="TTS audio exceeds the size or alignment limit")
            else:
                self.audio.extend(frame.audio)
        await self.push_frame(frame, direction)

    def wav(self) -> bytes:
        if not self.audio:
            raise VoiceError("empty_audio", "The speech service returned no audio.")
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(self.sample_rate)
            writer.writeframes(self.audio)
        return output.getvalue()


async def run_pipeline(
    processors: list[FrameProcessor],
    frames: list[Frame],
    *,
    finished: asyncio.Event | None = None,
) -> None:
    """Run real Pipecat lifecycle, propagate provider errors, always tear down."""
    worker = PipelineWorker(
        Pipeline(processors),
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
        enable_rtvi=False,
        enable_turn_tracking=False,
        idle_timeout_secs=None,
        cancel_timeout_secs=2,
    )
    failed = asyncio.Event()

    @worker.event_handler("on_pipeline_error")
    async def on_error(_worker, _frame):
        # Vendor exception bodies can contain user text or credentials. The
        # endpoint reports a stage and code; no raw exception is serialized.
        failed.set()

    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    running = asyncio.create_task(runner.run(worker))
    failure = asyncio.create_task(failed.wait())
    finalization = asyncio.create_task(finished.wait()) if finished else None
    try:
        await worker.queue_frames(frames)
        if finalization:
            await asyncio.wait(
                [running, failure, finalization], return_when=asyncio.FIRST_COMPLETED
            )
            if failed.is_set():
                raise VoiceError(
                    "provider_error", "The speech provider could not process the turn."
                )
            if not finished.is_set():
                await running
                raise VoiceError(
                    "provider_error", "The speech pipeline stopped before finalization."
                )
        await worker.stop_when_done()
        await asyncio.wait([running, failure], return_when=asyncio.FIRST_COMPLETED)
        if failed.is_set():
            raise VoiceError("provider_error", "The speech provider could not process the turn.")
        await running
    finally:
        if not running.done():
            await worker.cancel(reason="utterance closed")
        try:
            async with asyncio.timeout(3):
                await asyncio.shield(running)
        except (TimeoutError, asyncio.CancelledError):
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        failure.cancel()
        if finalization:
            finalization.cancel()
        await asyncio.gather(
            failure, *([finalization] if finalization else []), return_exceptions=True
        )


@dataclass(frozen=True)
class Reply:
    transcript: str
    text: str
    wav: bytes


class VoiceEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.history: deque[tuple[str, str]] = deque(maxlen=settings.history_turns)
        self.last_turn_at = 0.0
        self.evidence = {"stt_turns": 0, "llm_turns": 0, "tts_turns": 0}

    def build_stt(self) -> FrameProcessor:
        cfg = self.settings
        return UtteranceDeepgram(
            api_key=cfg.deepgram_api_key.get_secret_value(),
            sample_rate=16000,
            settings=DeepgramSTTService.Settings(
                model=cfg.deepgram_model,
                interim_results=True,
                smart_format=True,
                profanity_filter=False,
                endpointing=False,
            ),
        )

    def build_tts(self) -> FrameProcessor:
        cfg = self.settings
        return CartesiaHttpTTSService(
            api_key=cfg.cartesia_api_key.get_secret_value(),
            sample_rate=24000,
            cartesia_version="2024-06-10",
            settings=CartesiaHttpTTSService.Settings(
                model=cfg.cartesia_model, voice=cfg.cartesia_voice_id
            ),
        )

    def build_agent(self) -> Any:
        cfg = self.settings
        model = cfg.llm_model
        if not model.startswith("openrouter/"):
            model = "openrouter/" + model
        return LlmAgent(
            model=model,
            api_key=cfg.llm_api_key.get_secret_value(),
            tools=[],
            config=LlmConfig(
                system_prompt=PROMPT,
                temperature=0.5,
                max_tokens=250,
                num_retries=0,
                timeout=cfg.llm_timeout_seconds,
            ),
            backend="http",
        )

    async def transcribe(self, pcm: bytes) -> str:
        collector = TranscriptCollector(self.settings.max_text_chars)
        frames = [
            BufferedAudioFrame(pcm[offset : offset + 3200]) for offset in range(0, len(pcm), 3200)
        ]
        frames.append(FinishInputFrame())
        async with asyncio.timeout(self.settings.stt_timeout_seconds):
            await run_pipeline([self.build_stt(), collector], frames, finished=collector.finished)
        transcript = " ".join(collector.parts).strip()
        if not transcript:
            raise VoiceError("no_speech", "I did not hear any speech. Please try again.")
        self.evidence["stt_turns"] += 1
        return transcript

    async def answer(self, transcript: str) -> str:
        if time.monotonic() - self.last_turn_at > self.settings.history_ttl_seconds:
            self.history.clear()
        events = []
        for user, assistant in self.history:
            events.extend([UserTextSent(content=user), AgentTextSent(content=assistant)])
        user_text = UserTextSent(content=transcript)
        turn = UserTurnEnded(content=[user_text])
        events.extend([user_text, turn.model_copy()])
        turn.history = events
        agent = self.build_agent()
        parts: list[str] = []
        try:
            async with asyncio.timeout(self.settings.llm_timeout_seconds):
                async for event in agent.process(TurnEnv(AgentEnv()), turn, history=events):
                    if isinstance(event, AgentSendText):
                        parts.append(event.text)
                        if sum(map(len, parts)) > self.settings.max_text_chars:
                            raise VoiceError("response_too_large", "The answer exceeded its limit.")
        finally:
            await agent.cleanup()
        text = "".join(parts).strip()
        if not text:
            raise VoiceError("empty_answer", "The assistant returned no answer. Please try again.")
        self.evidence["llm_turns"] += 1
        return text

    async def synthesize(self, text: str) -> bytes:
        collector = AudioCollector(self.settings.max_output_bytes - 44)
        async with asyncio.timeout(self.settings.tts_timeout_seconds):
            await run_pipeline([self.build_tts(), collector], [TTSSpeakFrame(text)])
        wav = collector.wav()
        self.evidence["tts_turns"] += 1
        return wav

    async def process(self, pcm: bytes) -> Reply:
        transcript = await self.transcribe(pcm)
        text = await self.answer(transcript)
        return Reply(transcript, text, await self.synthesize(text))

    def remember(self, reply: Reply) -> None:
        # Commit conversation only after response_end was sent successfully.
        # The protocol has no playback acknowledgement; do not claim hearing it.
        self.history.append((reply.transcript, reply.text))
        self.last_turn_at = time.monotonic()

    def reset(self) -> None:
        self.history.clear()
        self.last_turn_at = 0.0
