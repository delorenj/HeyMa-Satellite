"""The single-satellite v0.1 PCM/WAV websocket boundary."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError

from tonny_voice.config import Settings
from tonny_voice.loopback import LoopbackEngine, VoiceError
from tonny_voice.wake import WakeDetection, WakeDetector, WakeDetectorError, WakeScorer

if TYPE_CHECKING:
    from tonny_voice.pipeline import VoiceEngine

log = logging.getLogger("tonny_voice")
STATIC_DIR = Path(__file__).parent / "static"
WAKE_FRAME_BYTES = 2_560


class Hello(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["hello"]
    session_id: str
    sample_rate: Literal[16000]
    encoding: Literal["pcm_s16le"]
    channels: Literal[1]
    client: Literal["heyma", "tonny"] = "tonny"
    version: Literal["0.1.0"] = "0.1.0"
    mode: Literal["turn", "continuous"] = "turn"


def control(message: dict) -> dict:
    text = message.get("text")
    if text is None:
        raise VoiceError("invalid_state", "Expected a JSON control message.")
    if len(text) > 2048:
        raise VoiceError("protocol_error", "Control message is too large.")
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise VoiceError("protocol_error", "Invalid JSON control message.") from exc
    if not isinstance(payload, dict):
        raise VoiceError("protocol_error", "Control message must be an object.")
    return payload


async def receive(ws: WebSocket) -> dict:
    message = await ws.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    return message


async def read_input(ws: WebSocket, cfg: Settings) -> bytes:
    audio = bytearray()
    async with asyncio.timeout(cfg.input_timeout_seconds):
        while True:
            message = await receive(ws)
            if message.get("bytes") is not None:
                chunk = message["bytes"]
                if not chunk or len(chunk) % 2:
                    raise VoiceError(
                        "invalid_audio", "PCM frames must contain whole 16-bit samples."
                    )
                if len(chunk) > cfg.max_frame_bytes:
                    raise VoiceError("frame_too_large", "The PCM frame exceeds the frame limit.")
                if len(audio) + len(chunk) > cfg.max_input_bytes:
                    raise VoiceError(
                        "input_too_large", "The utterance exceeds the audio duration limit."
                    )
                audio.extend(chunk)
                continue
            payload = control(message)
            if payload == {"type": "close"}:
                raise WebSocketDisconnect(1000)
            if payload != {"type": "end_of_input"}:
                raise VoiceError("invalid_state", "Expected PCM or end_of_input.")
            if not audio:
                raise VoiceError("no_speech", "The utterance contains no audio.")
            return bytes(audio)


def pcm_frame(message: dict, cfg: Settings) -> bytes:
    chunk = message.get("bytes")
    if chunk is None:
        raise VoiceError("invalid_state", "Expected a PCM frame.")
    if not chunk or len(chunk) % 2:
        raise VoiceError("invalid_audio", "PCM frames must contain whole 16-bit samples.")
    if len(chunk) > cfg.max_frame_bytes:
        raise VoiceError("frame_too_large", "The PCM frame exceeds the frame limit.")
    return chunk


def append_preroll(buffer: deque[bytes], size: int, chunk: bytes, limit: int) -> int:
    buffer.append(chunk)
    size += len(chunk)
    while buffer and size - len(buffer[0]) >= limit:
        size -= len(buffer.popleft())
    if size > limit:
        trim = size - limit
        buffer[0] = buffer[0][trim:]
        size = limit
    return size


async def score_wake_frame(detector: WakeScorer, frame: bytes) -> float:
    """Finish an in-flight stateful inference before allowing another session."""
    inference = asyncio.create_task(asyncio.to_thread(detector.score, frame))
    try:
        return await asyncio.shield(inference)
    except asyncio.CancelledError:
        await asyncio.gather(inference, return_exceptions=True)
        raise


async def receive_continuous_pcm(ws: WebSocket, cfg: Settings) -> bytes:
    message = await receive(ws)
    if message.get("bytes") is not None:
        return pcm_frame(message, cfg)
    if control(message) == {"type": "close"}:
        raise WebSocketDisconnect(1000)
    raise VoiceError("invalid_state", "Continuous mode accepts PCM frames only.")


async def read_continuous(
    ws: WebSocket,
    cfg: Settings,
    detector: WakeScorer,
    on_detected: Callable[[WakeDetection], None] | None = None,
) -> tuple[WakeDetection, bytes]:
    """Detect upstream, retain bounded pre-roll, then collect a fixed request window."""
    detector.reset()
    preroll: deque[bytes] = deque()
    preroll_size = 0
    pending = bytearray()
    consecutive = 0
    detection: WakeDetection | None = None
    initial_post = b""
    async with asyncio.timeout(cfg.continuous_timeout_seconds):
        while detection is None:
            pending.extend(await receive_continuous_pcm(ws, cfg))
            consumed = 0
            while len(pending) - consumed >= WAKE_FRAME_BYTES:
                frame = bytes(pending[consumed : consumed + WAKE_FRAME_BYTES])
                consumed += WAKE_FRAME_BYTES
                preroll_size = append_preroll(
                    preroll, preroll_size, frame, cfg.wake_preroll_bytes
                )
                try:
                    score = await score_wake_frame(detector, frame)
                except WakeDetectorError as exc:
                    raise VoiceError(
                        "wake_failed", "Wake detection could not process audio."
                    ) from exc
                consecutive = consecutive + 1 if score >= cfg.wake_threshold else 0
                if consecutive >= cfg.wake_trigger_frames:
                    detection = WakeDetection(detector.model_name, score)
                    initial_post = bytes(pending[consumed:])
                    break
            if consumed:
                del pending[:consumed]

    await ws.send_json(
        {"type": "wake_detected", "model": detection.model, "score": detection.score}
    )
    if on_detected is not None:
        on_detected(detection)
    audio = bytearray(b"".join(preroll))
    post_bytes = min(len(initial_post), cfg.wake_post_bytes)
    audio.extend(initial_post[:post_bytes])
    async with asyncio.timeout(cfg.wake_post_seconds + 5):
        while post_bytes < cfg.wake_post_bytes:
            chunk = await receive_continuous_pcm(ws, cfg)
            take = min(len(chunk), cfg.wake_post_bytes - post_bytes)
            audio.extend(chunk[:take])
            post_bytes += take
    if not audio or len(audio) > cfg.max_input_bytes:
        raise VoiceError("input_too_large", "The utterance exceeds the audio duration limit.")
    return detection, bytes(audio)


async def drain_continuous_input(ws: WebSocket, cfg: Settings) -> None:
    """Apply protocol validation while discarding live PCM during provider work."""
    while True:
        message = await receive(ws)
        if message.get("bytes") is not None:
            pcm_frame(message, cfg)
            continue
        if control(message) == {"type": "close"}:
            raise WebSocketDisconnect(1000)
        raise VoiceError("invalid_state", "Continuous mode accepts PCM frames only.")


async def respond(
    ws: WebSocket,
    engine: VoiceEngine | LoopbackEngine,
    pcm: bytes,
    *,
    continuous: bool = False,
) -> None:
    """Monitor the receive side while providers run; a lost Pi cancels the work."""
    processing = asyncio.create_task(engine.process(pcm))
    incoming = asyncio.create_task(
        drain_continuous_input(ws, engine.settings) if continuous else receive(ws)
    )
    try:
        async with asyncio.timeout(engine.settings.response_timeout_seconds):
            await asyncio.wait([processing, incoming], return_when=asyncio.FIRST_COMPLETED)
            if incoming.done():
                message = incoming.result()  # raises on disconnect
                if continuous:
                    raise VoiceError("invalid_state", "Continuous input ended unexpectedly.")
                if control(message) == {"type": "close"}:
                    raise WebSocketDisconnect(1000)
                raise VoiceError("invalid_state", "Input already ended; wait for the response.")
            reply = processing.result()
            await ws.send_json({"type": "response_start", "format": "wav", "final": True})
            for offset in range(0, len(reply.wav), 65536):
                await ws.send_bytes(reply.wav[offset : offset + 65536])
            await ws.send_json({"type": "response_end"})
            engine.remember(reply)
    finally:
        processing.cancel()
        incoming.cancel()
        await asyncio.gather(processing, incoming, return_exceptions=True)


def configure_logging() -> None:
    # SDK DEBUG logs include transcript text. Initialize in the application
    # factory as well as the CLI so reload workers retain the same INFO policy.
    from loguru import logger

    logger.remove()
    logger.add(lambda message: print(message, end=""), level="INFO")
    logging.basicConfig(level=logging.INFO)


def create_app(
    settings: Settings | None = None,
    *,
    engine: VoiceEngine | LoopbackEngine | None = None,
    wake_detector: WakeScorer | None = None,
) -> FastAPI:
    configure_logging()
    cfg = settings or Settings()
    if engine is not None:
        voice = engine
    elif cfg.mode == "loopback":
        voice = LoopbackEngine(cfg)
    else:
        # Offline loopback never imports provider SDKs or constructs their services.
        from tonny_voice.pipeline import VoiceEngine

        voice = VoiceEngine(cfg)
    wake = wake_detector
    if wake is None and cfg.mode == "live" and cfg.wake_enabled:
        wake = WakeDetector(cfg)
    app = FastAPI(title="Tonny Voice", docs_url=None, redoc_url=None)
    app.state.engine = voice
    app.state.active = False
    app.state.counters = {
        "sessions": 0,
        "responses_sent": 0,
        "wake_detected": 0,
        "errors": 0,
        "disconnects": 0,
    }
    app.state.last_error = None

    @app.get("/healthz")
    async def health():
        return {
            "ok": True,
            "service": "tonny-voice",
            "mode": cfg.mode,
            "commit": cfg.revision or cfg.commit,
            "revision": cfg.revision or cfg.commit,
            "configured": cfg.configured,
            "active_session": app.state.active,
            "pipeline": (
                "pcm -> wav-loopback"
                if cfg.mode == "loopback"
                else "pipecat-deepgram -> cartesia-line-llm -> pipecat-cartesia-tts"
            ),
            "models": {
                "stt": cfg.deepgram_model,
                "llm": cfg.llm_model,
                "tts": cfg.cartesia_model,
                "voice_id": cfg.cartesia_voice_id,
                "wake": wake.model_name if wake else None,
            },
            "wake": {
                "enabled": cfg.wake_enabled,
                "loaded": wake is not None,
                "model": wake.model_name if wake else None,
                "threshold": cfg.wake_threshold,
                "sha256": cfg.wake_model_sha256 if wake else None,
            },
            "versions": {
                "pipecat-ai": version("pipecat-ai"),
                "cartesia-line": version("cartesia-line"),
                "openwakeword": version("openwakeword"),
            },
            "evidence_since_start": {**app.state.counters, **voice.evidence},
            "last_error": app.state.last_error,
            "conversation_turns": len(voice.history),
            "audio_playback": "not_acknowledged_by_protocol",
        }

    @app.post("/v1/reset")
    async def reset():
        if app.state.active:
            raise HTTPException(409, "Wait until the active turn finishes.")
        voice.reset()
        return {"ok": True, "conversation_turns": 0}

    @app.websocket("/v1/voice")
    async def websocket(ws: WebSocket):
        await ws.accept()
        if app.state.active:
            await ws.send_json({"type": "error", "code": "busy", "message": "Tonny is busy."})
            await ws.close(code=1013)
            return
        app.state.active = True
        session = "unidentified"
        try:
            async with asyncio.timeout(cfg.hello_timeout_seconds):
                payload = control(await receive(ws))
                if payload.get("type") != "hello":
                    raise VoiceError("invalid_state", "The first message must be hello.")
                try:
                    hello = Hello.model_validate(payload)
                    identifier = UUID(hello.session_id)
                    if identifier.version != 4:
                        raise ValueError("session_id must be UUID4")
                    if (
                        type(payload.get("sample_rate")) is not int
                        or type(payload.get("channels")) is not int
                    ):
                        raise ValueError("audio format fields must be integers")
                except (ValidationError, ValueError, TypeError) as exc:
                    raise VoiceError(
                        "protocol_error",
                        "Expected UUID4 session, PCM S16_LE 16000 Hz mono, v0.1.0.",
                    ) from exc
            session = hello.session_id
            if cfg.mode == "live" and not all(cfg.configured.values()):
                raise VoiceError(
                    "not_configured", "The gateway's voice providers are not configured."
                )
            app.state.counters["sessions"] += 1
            await ws.send_json({"type": "ready", "session_id": session})
            if hello.mode == "continuous":
                if wake is None:
                    raise VoiceError("wake_unavailable", "Wake detection is not available.")

                def record_wake(detection: WakeDetection) -> None:
                    app.state.counters["wake_detected"] += 1
                    log.info(
                        "session=%s wake_detected model=%s score=%.6f",
                        session,
                        detection.model,
                        detection.score,
                    )

                _, pcm = await read_continuous(ws, cfg, wake, record_wake)
                await respond(ws, voice, pcm, continuous=True)
            else:
                pcm = await read_input(ws, cfg)
                await respond(ws, voice, pcm)
            app.state.counters["responses_sent"] += 1
            app.state.last_error = None
            log.info("session=%s response_sent input_bytes=%s", session, len(pcm))
        except WebSocketDisconnect:
            app.state.counters["disconnects"] += 1
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                error = VoiceError("timeout", "The voice turn timed out. Please try again.")
            elif isinstance(exc, VoiceError):
                error = exc
            else:
                error = VoiceError("internal", "The voice service could not complete this turn.")
            app.state.counters["errors"] += 1
            app.state.last_error = {"code": error.code, "exception_type": type(exc).__name__}
            log.warning("session=%s code=%s exception=%s", session, error.code, type(exc).__name__)
            with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError):
                await ws.send_json({"type": "error", "code": error.code, "message": str(error)})
        finally:
            app.state.active = False
            with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError):
                await ws.close()

    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Tonny Pipecat + Line voice gateway")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18778)
    parser.add_argument("--reload", action="store_true", help="Reload when Python source changes")
    parser.add_argument(
        "--reload-dir", action="append", metavar="PATH", help="Directory to watch (repeatable)"
    )
    args = parser.parse_args()
    configure_logging()
    uvicorn.run(
        "tonny_voice.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=(args.reload_dir or [str(Path(__file__).parent)]) if args.reload else None,
        ws_max_size=262144,
        ws_max_queue=16,
        limit_concurrency=16,
    )
