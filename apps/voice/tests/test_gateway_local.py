import importlib
import io
import os
import socket
import struct
import subprocess
import sys
import wave
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from tonny_voice import app as gateway
from tonny_voice.config import Settings
from tonny_voice.loopback import LoopbackEngine


@pytest.fixture
def local_settings():
    return Settings(
        mode="loopback",
        deepgram_api_key=SecretStr(""),
        cartesia_api_key=SecretStr(""),
        llm_api_key=SecretStr(""),
    )


def greeting():
    return {
        "type": "hello",
        "session_id": str(uuid4()),
        "sample_rate": 16000,
        "encoding": "pcm_s16le",
        "channels": 1,
    }


def loopback_turn(client, pcm):
    with client.websocket_connect("/v1/voice") as ws:
        hello = greeting()
        ws.send_json(hello)
        assert ws.receive_json() == {"type": "ready", "session_id": hello["session_id"]}
        for offset in range(0, len(pcm), 16384):
            ws.send_bytes(pcm[offset : offset + 16384])
        ws.send_json({"type": "end_of_input"})
        assert ws.receive_json() == {"type": "response_start", "format": "wav", "final": True}
        data = bytearray()
        while True:
            message = ws.receive()
            if "bytes" in message:
                data.extend(message["bytes"])
            else:
                assert message["text"] == '{"type":"response_end"}'
                return bytes(data)


def test_loopback_returns_exact_samples_without_providers_or_conversation(
    local_settings, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("Loopback must not construct providers or open a network connection")

    # Exercise the public websocket with every live provider constructor armed
    # to fail, even though the normal loopback path never imports this module.
    from tonny_voice import pipeline

    for name in ("UtteranceDeepgram", "CartesiaHttpTTSService", "LlmAgent", "VoiceEngine"):
        monkeypatch.setattr(pipeline, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    app = gateway.create_app(local_settings)
    pcm = struct.pack("<5h", 0, -32768, 32767, 1, -1) * 10000
    with TestClient(app) as client:
        for _ in range(2):
            result = loopback_turn(client, pcm)
            with wave.open(io.BytesIO(result)) as audio:
                assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (
                    16000,
                    1,
                    2,
                )
                assert audio.readframes(audio.getnframes()) == pcm
        health = client.get("/healthz").json()
        assert health["mode"] == "loopback"
        assert health["pipeline"] == "pcm -> wav-loopback"
        assert not any(health["configured"].values())
        assert health["evidence_since_start"]["responses_sent"] == 2
        assert health["evidence_since_start"]["sessions"] == 2
        for stage in ("stt_turns", "llm_turns", "tts_turns"):
            assert health["evidence_since_start"][stage] == 0
        assert health["conversation_turns"] == 0
        assert health["audio_playback"] == "not_acknowledged_by_protocol"
        assert client.post("/v1/reset").json() == {"ok": True, "conversation_turns": 0}
        assert list(app.state.engine.history) == []


async def test_loopback_reply_has_no_transcript_or_answer(local_settings):
    engine = LoopbackEngine(local_settings)
    reply = await engine.process(b"\x01\x00\xff\xff")
    assert reply.transcript is None
    assert reply.text is None
    engine.remember(reply)
    assert not engine.history


def test_loopback_import_does_not_load_provider_sdks():
    # A fresh interpreter verifies this even when test collection elsewhere
    # has already imported the live Pipecat engine in this interpreter.
    script = """
import builtins
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'pipecat', 'line', 'deepgram'}:
        raise AssertionError('Loopback imported provider SDK: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
from tonny_voice.app import create_app
from tonny_voice.config import Settings
app = create_app(Settings(mode='loopback'))
assert app.state.engine.__class__.__name__ == 'LoopbackEngine'
"""
    env = {name: value for name, value in os.environ.items() if not name.startswith("TONNY_")}
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=15
    )
    assert result.returncode == 0, result.stderr


def test_live_remains_default_and_requires_credentials(local_settings, monkeypatch):
    monkeypatch.delenv("TONNY_MODE", raising=False)
    cfg = Settings(
        deepgram_api_key=SecretStr(""),
        cartesia_api_key=SecretStr(""),
        llm_api_key=SecretStr(""),
    )
    app = gateway.create_app(cfg)
    with TestClient(app) as client, client.websocket_connect("/v1/voice") as ws:
        ws.send_json(greeting())
        assert ws.receive_json()["code"] == "not_configured"
        health = client.get("/healthz").json()
        assert health["mode"] == "live"
        assert health["pipeline"] == "pipecat-deepgram -> cartesia-line-llm -> pipecat-cartesia-tts"
        assert health["evidence_since_start"]["responses_sent"] == 0


def test_unknown_mode_is_rejected_before_serving(monkeypatch):
    monkeypatch.setenv("TONNY_MODE", "pretend-live")
    with pytest.raises(ValidationError):
        gateway.create_app()


def test_loopback_output_limit_is_enforced(local_settings):
    cfg = local_settings.model_copy(update={"max_output_bytes": 4096})
    with TestClient(gateway.create_app(cfg)) as client:
        with client.websocket_connect("/v1/voice") as ws:
            ws.send_json(greeting())
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(b"\0\0" * 2400)
            ws.send_json({"type": "end_of_input"})
            assert ws.receive_json()["code"] == "response_too_large"
        assert client.get("/healthz").json()["conversation_turns"] == 0


def test_index_and_static_files_are_served_without_shadowing_api(
    local_settings, tmp_path, monkeypatch
):
    (tmp_path / "index.html").write_text("<!doctype html><title>Loopback fixture</title>")
    (tmp_path / "app.js").write_text("window.loopbackFixture = true;")
    monkeypatch.setattr(gateway, "STATIC_DIR", tmp_path)
    with TestClient(gateway.create_app(local_settings)) as client:
        assert "Loopback fixture" in client.get("/").text
        assert client.get("/").headers["content-type"].startswith("text/html")
        assert client.get("/static/app.js").text == "window.loopbackFixture = true;"
        assert client.get("/static/missing.js").status_code == 404
        assert client.get("/healthz").json()["mode"] == "loopback"
        assert client.post("/v1/reset").status_code == 200


def test_cli_reload_uses_importable_factory_and_preserves_worker_logging(monkeypatch, capsys):
    invoked = {}
    monkeypatch.setattr(
        gateway.uvicorn, "run", lambda app, **kwargs: invoked.update(app=app, **kwargs)
    )
    monkeypatch.setattr(
        sys, "argv", ["tonny-voice", "--reload", "--reload-dir", "/tmp/tonny-source"]
    )
    gateway.main()
    assert invoked["app"] == "tonny_voice.app:create_app"
    assert invoked["factory"] is True
    assert invoked["reload"] is True
    assert invoked["reload_dirs"] == ["/tmp/tonny-source"]
    monkeypatch.setenv("TONNY_MODE", "loopback")
    module, name = invoked["app"].split(":")
    app = getattr(importlib.import_module(module), name)()
    assert isinstance(app.state.engine, LoopbackEngine)
    from loguru import logger

    logger.debug("should-not-leak-worker-debug")
    logger.info("reload-worker-info")
    output = capsys.readouterr().out
    assert "reload-worker-info" in output
    assert "should-not-leak-worker-debug" not in output
