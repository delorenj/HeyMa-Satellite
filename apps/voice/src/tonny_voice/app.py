"""The single-satellite v0.1 PCM/WAV websocket boundary."""

import argparse
import asyncio
import contextlib
import json
import logging
from importlib.metadata import version
from typing import Literal
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, ValidationError

from tonny_voice.config import Settings
from tonny_voice.pipeline import VoiceEngine, VoiceError

log = logging.getLogger("tonny_voice")


class Hello(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["hello"]
    session_id: str
    sample_rate: Literal[16000]
    encoding: Literal["pcm_s16le"]
    channels: Literal[1]
    client: Literal["heyma", "tonny"] = "tonny"
    version: Literal["0.1.0"] = "0.1.0"


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


async def respond(ws: WebSocket, engine: VoiceEngine, pcm: bytes) -> None:
    """Monitor the receive side while providers run; a lost Pi cancels the work."""
    processing = asyncio.create_task(engine.process(pcm))
    incoming = asyncio.create_task(receive(ws))
    try:
        async with asyncio.timeout(engine.settings.response_timeout_seconds):
            await asyncio.wait([processing, incoming], return_when=asyncio.FIRST_COMPLETED)
            if incoming.done():
                message = incoming.result()  # raises on disconnect
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


def create_app(settings: Settings | None = None, *, engine: VoiceEngine | None = None) -> FastAPI:
    cfg = settings or Settings()
    voice = engine or VoiceEngine(cfg)
    app = FastAPI(title="Tonny Voice", docs_url=None, redoc_url=None)
    app.state.engine = voice
    app.state.active = False
    app.state.counters = {"sessions": 0, "responses_sent": 0, "errors": 0, "disconnects": 0}
    app.state.last_error = None

    @app.get("/healthz")
    async def health():
        return {
            "ok": True,
            "service": "tonny-voice",
            "commit": cfg.revision or cfg.commit,
            "revision": cfg.revision or cfg.commit,
            "configured": cfg.configured,
            "active_session": app.state.active,
            "pipeline": "pipecat-deepgram -> cartesia-line-llm -> pipecat-cartesia-tts",
            "models": {
                "stt": cfg.deepgram_model,
                "llm": cfg.llm_model,
                "tts": cfg.cartesia_model,
                "voice_id": cfg.cartesia_voice_id,
            },
            "versions": {
                "pipecat-ai": version("pipecat-ai"),
                "cartesia-line": version("cartesia-line"),
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
            if not all(cfg.configured.values()):
                raise VoiceError(
                    "not_configured", "The gateway's voice providers are not configured."
                )
            app.state.counters["sessions"] += 1
            await ws.send_json({"type": "ready", "session_id": session})
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

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Tonny Pipecat + Line voice gateway")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18778)
    args = parser.parse_args()
    # SDK DEBUG logs include transcript text. Keep operational evidence concise.
    from loguru import logger

    logger.remove()
    logger.add(lambda message: print(message, end=""), level="INFO")
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        ws_max_size=262144,
        ws_max_queue=16,
        limit_concurrency=16,
    )
