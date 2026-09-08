import asyncio
import time
from uuid import uuid4

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from tonny_voice.app import create_app, read_continuous
from tonny_voice.pipeline import Reply, VoiceEngine


def hello(**updates):
    return {
        "type": "hello",
        "session_id": str(uuid4()),
        "sample_rate": 16000,
        "encoding": "pcm_s16le",
        "channels": 1,
        "client": "tonny",
        "version": "0.1.0",
        **updates,
    }


@pytest.mark.parametrize(
    "first,code",
    [
        ("{bad", "protocol_error"),
        ("[]", "protocol_error"),
        ({"type": "end_of_input"}, "invalid_state"),
        (hello(sample_rate=48000), "protocol_error"),
        (hello(channels=True), "protocol_error"),
        (hello(encoding="float32"), "protocol_error"),
        (hello(session_id="not-uuid"), "protocol_error"),
        (hello(version="9.0.0"), "protocol_error"),
        (hello(mode="unknown"), "protocol_error"),
        (b"\x00\x00", "invalid_state"),
    ],
)
@pytest.mark.parametrize("mode", ["live", "loopback"])
def test_reject_invalid_handshakes(settings, first, code, mode):
    cfg = settings.model_copy(update={"mode": mode})
    with TestClient(create_app(cfg)) as client, client.websocket_connect("/v1/voice") as ws:
        if isinstance(first, bytes):
            ws.send_bytes(first)
        elif isinstance(first, str):
            ws.send_text(first)
        else:
            ws.send_json(first)
        assert ws.receive_json()["code"] == code


@pytest.mark.parametrize(
    "chunks,ending,code",
    [
        ([b"x"], None, "invalid_audio"),
        ([b""], None, "invalid_audio"),
        ([b"\0" * 65538], None, "frame_too_large"),
        ([b"\0" * 32000, b"\0\0"], None, "input_too_large"),
        ([], {"type": "end_of_input"}, "no_speech"),
        ([b"\0\0"], {"type": "surprise"}, "invalid_state"),
    ],
)
@pytest.mark.parametrize("mode", ["live", "loopback"])
def test_reject_malformed_audio_and_out_of_order_messages(settings, chunks, ending, code, mode):
    cfg = settings.model_copy(update={"max_input_seconds": 1, "mode": mode})
    with TestClient(create_app(cfg)) as client, client.websocket_connect("/v1/voice") as ws:
        greeting = hello()
        ws.send_json(greeting)
        assert ws.receive_json() == {"type": "ready", "session_id": greeting["session_id"]}
        for chunk in chunks:
            ws.send_bytes(chunk)
        if ending:
            ws.send_json(ending)
        assert ws.receive_json()["code"] == code


@pytest.mark.parametrize("mode", ["live", "loopback"])
def test_one_active_session_and_reset_conflict(settings, mode):
    cfg = settings.model_copy(update={"mode": mode})
    with TestClient(create_app(cfg)) as client, client.websocket_connect("/v1/voice") as first:
        first.send_json(hello())
        assert first.receive_json()["type"] == "ready"
        assert client.post("/v1/reset").status_code == 409
        with client.websocket_connect("/v1/voice") as second:
            assert second.receive_json()["code"] == "busy"


class Engine(VoiceEngine):
    def __init__(self, settings, stall=False):
        super().__init__(settings)
        self.stall = stall
        self.cancelled = False
        self.received = []

    async def process(self, pcm):
        self.received.append(pcm)
        if self.stall:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
        return Reply("Hello", "Hi", b"RIFF-test-WAV")


class FakeWake:
    model_name = "hey_tonny"

    def __init__(self, scores):
        self.scores = iter(scores)
        self.reset_calls = 0
        self.frames = []

    def reset(self):
        self.reset_calls += 1

    def score(self, pcm):
        self.frames.append(pcm)
        return next(self.scores, 0.0)


def test_complete_wire_turn_and_health_admit_actual_evidence(settings):
    engine = Engine(settings)
    app = create_app(settings, engine=engine)
    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert health["configured"] == dict.fromkeys(["deepgram", "openrouter", "cartesia"], True)
        assert health["evidence_since_start"]["responses_sent"] == 0
        assert health["evidence_since_start"]["tts_turns"] == 0
        with client.websocket_connect("/v1/voice") as ws:
            ws.send_json(hello())
            ws.receive_json()
            ws.send_bytes(b"\x01\x00")
            ws.send_bytes(b"\x02\x00")
            ws.send_json({"type": "end_of_input"})
            assert ws.receive_json() == {"type": "response_start", "format": "wav", "final": True}
            assert ws.receive_bytes() == b"RIFF-test-WAV"
            assert ws.receive_json() == {"type": "response_end"}
        assert engine.received == [b"\x01\x00\x02\x00"]
        assert list(engine.history) == [("Hello", "Hi")]
        assert client.get("/healthz").json()["evidence_since_start"]["responses_sent"] == 1
        assert client.post("/v1/reset").json()["conversation_turns"] == 0


def test_continuous_mode_detects_upstream_and_preserves_bounded_request_audio(settings):
    cfg = settings.model_copy(
        update={
            "wake_preroll_seconds": 0.08,
            "wake_post_seconds": 0.16,
            "wake_trigger_frames": 1,
            "max_input_seconds": 1,
        }
    )
    detector = FakeWake([0.0, 0.9])
    engine = Engine(cfg)
    app = create_app(cfg, engine=engine, wake_detector=detector)
    frames = [bytes([index, 0]) * 1_280 for index in range(1, 5)]
    with TestClient(app) as client, client.websocket_connect("/v1/voice") as ws:
        greeting = hello(mode="continuous")
        ws.send_json(greeting)
        assert ws.receive_json() == {"type": "ready", "session_id": greeting["session_id"]}
        for frame in frames:
            ws.send_bytes(frame)
        assert ws.receive_json() == {
            "type": "wake_detected",
            "model": "hey_tonny",
            "score": 0.9,
        }
        assert ws.receive_json() == {"type": "response_start", "format": "wav", "final": True}
        assert ws.receive_bytes() == b"RIFF-test-WAV"
        assert ws.receive_json() == {"type": "response_end"}
        health = client.get("/healthz").json()
    assert detector.reset_calls == 1
    assert engine.received == [b"".join(frames[1:])]
    assert health["evidence_since_start"]["wake_detected"] == 1
    assert health["evidence_since_start"]["responses_sent"] == 1


@pytest.mark.parametrize("packet_size", [2, 5_120])
def test_continuous_mode_reframes_transport_packets_for_inference(settings, packet_size):
    cfg = settings.model_copy(
        update={
            "wake_preroll_seconds": 0.08,
            "wake_post_seconds": 0.08,
            "wake_trigger_frames": 1,
            "max_input_seconds": 1,
        }
    )
    detector = FakeWake([0.9])
    engine = Engine(cfg)
    app = create_app(cfg, engine=engine, wake_detector=detector)
    detection_frame = b"\1\0" * 1_280
    post_frame = b"\2\0" * 1_280
    stream = detection_frame + post_frame
    with TestClient(app) as client, client.websocket_connect("/v1/voice") as ws:
        ws.send_json(hello(mode="continuous"))
        assert ws.receive_json()["type"] == "ready"
        for offset in range(0, len(stream), packet_size):
            ws.send_bytes(stream[offset : offset + packet_size])
        assert ws.receive_json()["type"] == "wake_detected"
        assert ws.receive_json()["type"] == "response_start"
        assert ws.receive_bytes() == b"RIFF-test-WAV"
        assert ws.receive_json() == {"type": "response_end"}
    assert detector.frames == [detection_frame]
    assert engine.received == [stream]


class DelayedWake(FakeWake):
    def __init__(self, score, delay):
        super().__init__([score])
        self.delay = delay
        self.scoring = False
        self.reset_during_score = False

    def reset(self):
        self.reset_during_score |= self.scoring
        super().reset()

    def score(self, pcm):
        self.frames.append(pcm)
        self.scoring = True
        try:
            time.sleep(self.delay)
            return next(self.scores, 0.0)
        finally:
            self.scoring = False


class DirectWebSocket:
    def __init__(self, messages, delay=0):
        self.messages = iter(messages)
        self.delay = delay
        self.sent = []

    async def receive(self):
        message = next(self.messages)
        if self.delay and self.sent:
            await asyncio.sleep(self.delay)
        if isinstance(message, dict):
            return message
        return {"type": "websocket.receive", "bytes": message}

    async def send_json(self, message):
        self.sent.append(message)


@pytest.mark.asyncio
async def test_continuous_timeout_drains_inflight_stateful_inference(settings):
    cfg = settings.model_copy(update={"continuous_timeout_seconds": 0.01})
    detector = DelayedWake(0.0, 0.05)
    ws = DirectWebSocket([b"\0" * 2_560])
    with pytest.raises(TimeoutError):
        await read_continuous(ws, cfg, detector)
    detector.reset()
    assert not detector.scoring
    assert not detector.reset_during_score


@pytest.mark.asyncio
async def test_post_wake_capture_has_an_independent_deadline(settings):
    cfg = settings.model_copy(
        update={
            "continuous_timeout_seconds": 0.06,
            "wake_preroll_seconds": 0.08,
            "wake_post_seconds": 0.08,
            "wake_trigger_frames": 1,
            "max_input_seconds": 1,
        }
    )
    detector = DelayedWake(0.9, 0.04)
    detection_frame = b"\1\0" * 1_280
    post_frame = b"\2\0" * 1_280
    ws = DirectWebSocket([detection_frame, post_frame], delay=0.04)
    detection, audio = await read_continuous(ws, cfg, detector)
    assert detection.score == 0.9
    assert audio == detection_frame + post_frame


@pytest.mark.asyncio
async def test_wake_observation_precedes_failed_post_capture(settings):
    cfg = settings.model_copy(
        update={
            "wake_preroll_seconds": 0.08,
            "wake_post_seconds": 0.08,
            "wake_trigger_frames": 1,
            "max_input_seconds": 1,
        }
    )
    detector = FakeWake([0.9])
    observed = []
    ws = DirectWebSocket(
        [b"\1\0" * 1_280, {"type": "websocket.disconnect", "code": 1000}]
    )
    with pytest.raises(WebSocketDisconnect):
        await read_continuous(ws, cfg, detector, observed.append)
    assert len(observed) == 1
    assert observed[0].score == 0.9


def test_continuous_mode_drains_pcm_while_provider_runs(settings):
    cfg = settings.model_copy(
        update={
            "wake_preroll_seconds": 0.08,
            "wake_post_seconds": 0.08,
            "wake_trigger_frames": 1,
            "max_input_seconds": 1,
            "response_timeout_seconds": 0.05,
        }
    )
    engine = Engine(cfg, stall=True)
    app = create_app(cfg, engine=engine, wake_detector=FakeWake([0.9]))
    frame = b"\1\0" * 1_280
    with TestClient(app) as client, client.websocket_connect("/v1/voice") as ws:
        ws.send_json(hello(mode="continuous"))
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame)
        assert ws.receive_json()["type"] == "wake_detected"
        ws.send_bytes(frame)
        for _ in range(4):
            ws.send_bytes(frame)
        assert ws.receive_json()["code"] == "timeout"
    assert engine.cancelled
    assert engine.received == [frame + frame]


def test_continuous_mode_requires_loaded_gateway_detector(settings):
    app = create_app(settings, engine=Engine(settings))
    with TestClient(app) as client, client.websocket_connect("/v1/voice") as ws:
        ws.send_json(hello(mode="continuous"))
        assert ws.receive_json()["type"] == "ready"
        assert ws.receive_json()["code"] == "wake_unavailable"


@pytest.mark.parametrize("action", ["disconnect", "extra_input", "timeout"])
def test_disconnect_and_timeout_cancel_pending_provider_work(settings, action):
    cfg = settings.model_copy(update={"response_timeout_seconds": 0.15})
    engine = Engine(cfg, stall=True)
    app = create_app(cfg, engine=engine)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/voice") as ws:
            ws.send_json(hello())
            ws.receive_json()
            ws.send_bytes(b"\1\0" * 100)
            ws.send_json({"type": "end_of_input"})
            if action == "disconnect":
                ws.close()
            elif action == "extra_input":
                ws.send_bytes(b"\1\0")
                assert ws.receive_json()["code"] == "invalid_state"
            else:
                assert ws.receive_json()["code"] == "timeout"
        assert engine.cancelled
        assert not app.state.active
        assert list(engine.history) == []


@pytest.mark.parametrize("mode", ["live", "loopback"])
def test_idle_input_deadline_releases_single_session_slot(settings, mode):
    cfg = settings.model_copy(update={"input_timeout_seconds": 0.02, "mode": mode})
    with TestClient(create_app(cfg)) as client, client.websocket_connect("/v1/voice") as ws:
        ws.send_json(hello())
        ws.receive_json()
        assert ws.receive_json()["code"] == "timeout"
