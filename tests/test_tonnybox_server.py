import json
import pathlib
import sys
import wave
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.tonnybox_server import (  # noqa: E402
    DEFAULT_TRANSCRIPT_DIR,
    Settings,
    build_envelope,
    command_argv,
    fallback_wav,
    parse_hello_frame,
    parse_router_response,
    parse_transcript_output,
    pcm_to_wav,
    prompt_from_transcript,
    relocate_transcript_output,
    subject_and_domain,
    transcript_path_for,
)


def test_subject_and_domain_builds_versionless_nats_subject():
    subject, domain = subject_and_domain("bloodbank.audio.file.received", "event")

    assert subject == "bloodbank.evt.audio.file.received"
    assert domain == "audio"


def test_subject_and_domain_rejects_retired_versioned_type():
    # The retired 5-token type must not silently build a subject nothing binds.
    for retired in (
        "bloodbank.v1.audio.file.received",
        "bloodbank.v2.audio.file.received",
    ):
        try:
            subject_and_domain(retired, "event")
        except ValueError:
            continue
        raise AssertionError(f"expected {retired!r} to be rejected")


def test_command_envelope_has_workqueue_fields():
    env = build_envelope(
        "bloodbank.audio.transcription.start",
        {"id": "tx-1"},
        kind="command",
        command_id="cmd-1",
        idempotency_key="audio.transcription.start:session-1",
    )

    assert env["kind"] == "command"
    assert env["subject"] == "bloodbank.cmd.audio.transcription.start"
    assert env["command_id"] == "cmd-1"
    assert env["correlationid"] == "cmd-1"
    assert env["delivery"] == "single_consumer"
    assert env["idempotency_key"] == "audio.transcription.start:session-1"
    assert "ordering_key" not in env


def test_event_envelope_has_ordering_key():
    env = build_envelope(
        "bloodbank.conversation.message.appended",
        {"id": "msg-1", "text": "say good morning"},
        ordering_key="turn:abc",
    )

    assert env["kind"] == "event"
    assert env["subject"] == "bloodbank.evt.conversation.message.appended"
    assert env["ordering_key"] == "turn:abc"
    assert json.loads(json.dumps(env)) == env


def test_pcm_to_wav_writes_valid_mono_16_bit_wav(tmp_path):
    wav_bytes = pcm_to_wav(b"\x00\x00" * 160, sample_rate=16_000)
    wav_path = tmp_path / "sample.wav"
    wav_path.write_bytes(wav_bytes)

    with wave.open(str(wav_path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16_000
        assert wav.getnframes() == 160


def test_fallback_wav_is_playable_wav(tmp_path):
    wav_path = tmp_path / "fallback.wav"
    wav_path.write_bytes(fallback_wav("good morning"))

    with wave.open(str(wav_path), "rb") as wav:
        assert wav.getframerate() == 16_000
        assert wav.getnframes() > 0


def test_parse_transcript_output_extracts_markdown_body(tmp_path):
    transcript = tmp_path / "transcript.md"
    transcript.write_text(
        "# Transcription\n\n- **Source**: `x.wav`\n\n---\n\nsay good morning\n",
        encoding="utf-8",
    )

    assert parse_transcript_output(str(transcript)) == "say good morning"


def test_default_settings_route_transcripts_to_tonny_directory(monkeypatch):
    monkeypatch.delenv("TONNYBOX_TRANSCRIPT_DIR", raising=False)
    monkeypatch.delenv("TONNYBOX_TRANSCRIBE_COMMAND", raising=False)
    monkeypatch.delenv("TONNYBOX_REQUIRE_WAKEWORD", raising=False)

    settings = Settings.from_env()

    assert settings.transcript_dir == DEFAULT_TRANSCRIPT_DIR
    assert "{transcript_path}" in settings.transcribe_command
    assert "--no-diarization" not in settings.transcribe_command
    assert settings.require_wakeword is True
    assert settings.router_webhook_url == ""
    assert settings.router_timeout_s == 10
    assert settings.fast_ack_text == "Got it."


def test_transcript_path_for_uses_tonny_directory_and_safe_session():
    path = transcript_path_for(
        pathlib.Path("/tmp/TonnyTranscripts"),
        "session with spaces/and/slashes",
        now=datetime(2026, 7, 1, 12, 34, 56, tzinfo=timezone.utc),
    )

    assert path == pathlib.Path("/tmp/TonnyTranscripts/20260701T123456Z-session-with-spaces-and-slashes.md")


def test_transcribe_command_sets_remote_dir_and_explicit_output(monkeypatch):
    monkeypatch.delenv("TONNYBOX_TRANSCRIBE_COMMAND", raising=False)
    settings = Settings.from_env()
    argv = command_argv(
        settings.transcribe_command,
        audio_path="/tmp/source.wav",
        transcript_dir=str(DEFAULT_TRANSCRIPT_DIR),
        transcript_path=str(DEFAULT_TRANSCRIPT_DIR / "session.md"),
    )

    assert argv[:3] == [
        "env",
        f"TRANSCRIPTS={DEFAULT_TRANSCRIPT_DIR}",
        "/home/delorenj/code/HeyMa/bin/transcribe",
    ]
    assert "--output" in argv
    assert argv[argv.index("--output") + 1] == str(DEFAULT_TRANSCRIPT_DIR / "session.md")
    assert "--no-diarization" not in argv


def test_relocate_transcript_output_moves_legacy_transcript(tmp_path):
    legacy = tmp_path / "Transcripts" / "voice-note.md"
    legacy.parent.mkdir()
    legacy.write_text(
        "# Transcription\n\n- **Source**: `x.wav`\n\n---\n\nhey tonny say good morning\n",
        encoding="utf-8",
    )
    destination = tmp_path / "Notes" / "TonnyTranscripts" / "session.md"

    stdout = relocate_transcript_output(str(legacy), destination)

    assert stdout == str(destination)
    assert not legacy.exists()
    assert destination.exists()
    assert parse_transcript_output(stdout) == "hey tonny say good morning"


def test_systemd_unit_enforces_tonny_transcripts_and_pi_side_wakeword_gate():
    unit = (ROOT / "server" / "tonnybox-server.service").read_text(encoding="utf-8")

    assert "TONNYBOX_TRANSCRIPT_DIR=/home/delorenj/d/Notes/TonnyTranscripts" in unit
    assert "--output {transcript_path}" in unit
    assert "--no-diarization" not in unit
    assert "TONNYBOX_REQUIRE_WAKEWORD=false" in unit
    assert "TONNYBOX_ROUTER_WEBHOOK_URL=http://127.0.0.1:5678/webhook/tonny-operator-router" in unit
    assert "TONNYBOX_FAST_ACK_TEXT=Got it." in unit


def test_prompt_from_transcript_strips_wakeword_prefix():
    assert prompt_from_transcript("hey tonny, say good morning") == "say good morning"
    assert prompt_from_transcript("Hey Tony: say good morning") == "say good morning"
    assert prompt_from_transcript("Hey, Tommy say good morning") == "say good morning"
    assert prompt_from_transcript("room noise hey tonny, say good morning") == "say good morning"


def test_prompt_from_transcript_requires_wakeword_by_default():
    try:
        prompt_from_transcript("say good morning")
    except RuntimeError as exc:
        assert str(exc) == "transcription did not contain wakeword"
    else:
        raise AssertionError("expected RuntimeError")


def test_prompt_from_transcript_can_allow_legacy_prompt_only_transcripts():
    assert prompt_from_transcript("say good morning", require_wakeword=False) == "say good morning"


def test_parse_router_response_accepts_contract_payload():
    response = parse_router_response(
        {
            "route": "helpdesk",
            "agent_id": "tonny-helpdesk",
            "agent_display_name": "Tonny Help Desk",
            "response_text": "Help desk here.",
            "model": "stub-v1",
            "backend": "stub",
            "execution_id": "123",
        }
    )

    assert response.route == "helpdesk"
    assert response.agent_id == "tonny-helpdesk"
    assert response.agent_display_name == "Tonny Help Desk"
    assert response.response_text == "Help desk here."
    assert response.model == "stub-v1"
    assert response.backend == "stub"
    assert response.execution_id == "123"


def test_parse_router_response_accepts_n8n_item_wrapper_and_normalizes_unknown_route():
    response = parse_router_response(
        [
            {
                "json": {
                    "route": "not-a-real-route",
                    "response_text": "Operator fallback.",
                }
            }
        ]
    )

    assert response.route == "unknown"
    assert response.agent_id == "tonny-unknown"
    assert response.response_text == "Operator fallback."


def test_parse_router_response_requires_response_text():
    try:
        parse_router_response({"route": "conversation"})
    except RuntimeError as exc:
        assert str(exc) == "router response did not include response_text"
    else:
        raise AssertionError("expected RuntimeError")


def test_parse_hello_frame_defaults_session_and_sample_rate():
    session_id, sample_rate = parse_hello_frame('{"type": "hello"}')

    assert session_id.startswith("tonnybox-")
    assert sample_rate == 16_000


def test_parse_hello_frame_rejects_bad_sample_rate():
    try:
        parse_hello_frame('{"type": "hello", "sample_rate": 0}')
    except ValueError as exc:
        assert str(exc) == "sample_rate must be positive"
    else:
        raise AssertionError("expected ValueError")
