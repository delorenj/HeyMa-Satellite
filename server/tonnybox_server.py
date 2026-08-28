#!/usr/bin/env python3
"""Pi-facing TonnyBox voice gateway.

Accepts the existing HeyMa satellite websocket protocol, stores post-wake
utterance audio in MinIO/S3, emits Bloodbank audio/transcription/conversation
events, waits for Tonny's response event, and returns response WAV bytes to the
satellite for playback.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

try:
    import nats
except ImportError:  # pragma: no cover - deployment preflight catches this.
    nats = None


KIND_MARKERS = {"event": "evt", "command": "cmd", "reply": "rpy"}
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8778
DEFAULT_PATH = "/v1/voice"
DEFAULT_NATS_URL = "nats://127.0.0.1:4222"
DEFAULT_HERMES_AGENT_ID = "tonnybox-pm"
DEFAULT_MODEL = "gpt-codex-5.4"
DEFAULT_TRANSCRIBE_REPLY_TIMEOUT_S = "2"
DEFAULT_ROUTER_TIMEOUT_S = "10"
DEFAULT_FAST_ACK_TEXT = "Got it."
DEFAULT_TRANSCRIPT_DIR = pathlib.Path("/home/delorenj/d/Notes/TonnyTranscripts")
DEFAULT_TONNY_ERROR_REPLY = "Transcript saved. Tonny timed out before answering."
DEFAULT_ROUTER_ERROR_REPLY = "I heard you, but the Tonny operator router is not responding."
DEFAULT_TRANSCRIBE_COMMAND = (
    "env TRANSCRIPTS={transcript_dir} "
    "/home/delorenj/code/HeyMa/bin/transcribe {audio_path} --device cpu --output {transcript_path}"
)
WAKEWORD_RE = re.compile(r"\bhey[\s,.:;!?-]*(?:tonny|tony|tommy)\b", re.IGNORECASE)
ROUTES = {"conversation", "projects", "helpdesk", "home", "unknown"}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    path: str
    nats_url: str
    s3_alias: str
    s3_bucket: str
    s3_prefix: str
    mc_bin: str
    work_dir: pathlib.Path
    transcript_dir: pathlib.Path
    transcribe_command: str
    transcribe_reply_timeout_s: float
    tonny_reply_timeout_s: float
    router_webhook_url: str
    router_timeout_s: float
    tonny_agent_id: str
    tts_command: str
    hermes_model: str
    require_wakeword: bool
    tonny_error_reply: str
    router_error_reply: str
    fast_ack_text: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("TONNYBOX_SERVER_HOST", DEFAULT_HOST),
            port=int(os.environ.get("TONNYBOX_SERVER_PORT", str(DEFAULT_PORT))),
            path=os.environ.get("TONNYBOX_SERVER_PATH", DEFAULT_PATH),
            nats_url=os.environ.get("BLOODBANK_NATS_URL", DEFAULT_NATS_URL),
            s3_alias=os.environ.get("TONNYBOX_S3_ALIAS", "delo"),
            s3_bucket=os.environ.get("TONNYBOX_S3_BUCKET", "tonnybox"),
            s3_prefix=os.environ.get("TONNYBOX_S3_PREFIX", "utterances"),
            mc_bin=os.environ.get("MC_BIN", "/usr/local/bin/mc"),
            work_dir=pathlib.Path(os.environ.get("TONNYBOX_WORK_DIR", "/var/lib/tonnybox-server")),
            transcript_dir=pathlib.Path(
                os.environ.get("TONNYBOX_TRANSCRIPT_DIR", str(DEFAULT_TRANSCRIPT_DIR))
            ).expanduser(),
            transcribe_command=os.environ.get("TONNYBOX_TRANSCRIBE_COMMAND", DEFAULT_TRANSCRIBE_COMMAND),
            transcribe_reply_timeout_s=float(
                os.environ.get("TONNYBOX_TRANSCRIBE_REPLY_TIMEOUT_S", DEFAULT_TRANSCRIBE_REPLY_TIMEOUT_S)
            ),
            tonny_reply_timeout_s=float(os.environ.get("TONNYBOX_TONNY_REPLY_TIMEOUT_S", "300")),
            router_webhook_url=os.environ.get("TONNYBOX_ROUTER_WEBHOOK_URL", "").strip(),
            router_timeout_s=float(os.environ.get("TONNYBOX_ROUTER_TIMEOUT_S", DEFAULT_ROUTER_TIMEOUT_S)),
            tonny_agent_id=os.environ.get("TONNYBOX_TONNY_AGENT_ID", DEFAULT_HERMES_AGENT_ID),
            tts_command=os.environ.get("TONNYBOX_TTS_COMMAND", "voxxy speak --raw {text}"),
            hermes_model=os.environ.get("TONNYBOX_HERMES_MODEL", DEFAULT_MODEL),
            require_wakeword=os.environ.get("TONNYBOX_REQUIRE_WAKEWORD", "true").lower()
            not in {"0", "false", "no", "off"},
            tonny_error_reply=os.environ.get("TONNYBOX_TONNY_ERROR_REPLY", DEFAULT_TONNY_ERROR_REPLY),
            router_error_reply=os.environ.get("TONNYBOX_ROUTER_ERROR_REPLY", DEFAULT_ROUTER_ERROR_REPLY),
            fast_ack_text=os.environ.get("TONNYBOX_FAST_ACK_TEXT", DEFAULT_FAST_ACK_TEXT).strip(),
        )


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    transcript_path: str
    transcription_id: str


@dataclass(frozen=True)
class RouterResponse:
    route: str
    agent_id: str
    agent_display_name: str
    response_text: str
    model: str
    backend: str
    execution_id: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, **fields: Any) -> None:
    print(json.dumps({"time": utc_now(), "event": event, **fields}, sort_keys=True), flush=True)


def subject_and_domain(ce_type: str, kind: str) -> tuple[str, str]:
    """Map a Bloodbank CloudEvents type onto its NATS subject.

    The grammar is version-free:
        type    bloodbank.<domain>.<entity>.<action>          (4 tokens)
        subject bloodbank.<kind>.<domain>.<entity>.<action>   (5 tokens)

    The retired shape carried a `v1` token in both. Rejecting it here is
    deliberate: a 5-token type is now a bug at the call site, and failing
    loudly beats publishing onto a subject nothing binds.
    """
    parts = ce_type.split(".")
    if len(parts) != 4 or parts[0] != "bloodbank":
        raise ValueError(f"invalid Bloodbank type: {ce_type!r}")
    if kind not in KIND_MARKERS:
        raise ValueError(f"invalid Bloodbank kind: {kind!r}")
    _vendor, domain, entity, action = parts
    return f"bloodbank.{KIND_MARKERS[kind]}.{domain}.{entity}.{action}", domain


def build_envelope(
    ce_type: str,
    data: dict[str, Any],
    *,
    kind: str = "event",
    source: str = "urn:33god:tonnybox:server",
    producer: str = "tonnybox-server",
    service: str = "tonnybox-server",
    actor: dict[str, Any] | None = None,
    correlationid: str | None = None,
    causationid: str | None = None,
    ordering_key: str | None = None,
    command_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    subject, domain = subject_and_domain(ce_type, kind)
    event_id = str(uuid.uuid4())
    correlation = correlationid or event_id
    causation = causationid or correlation
    env: dict[str, Any] = {
        "specversion": "1.0",
        "type": ce_type,
        "id": event_id,
        "source": source,
        "subject": subject,
        "time": utc_now(),
        "datacontenttype": "application/json",
        "kind": kind,
        "producer": producer,
        "service": service,
        "domain": domain,
        "actor": actor or {"type": "service", "service": service},
        "data": data,
        "correlationid": correlation,
        "causationid": causation,
    }
    if kind == "event":
        env["ordering_key"] = ordering_key or f"{domain}:{data.get('id', correlation)}"
    if kind == "command":
        cmd_id = command_id or correlation
        env["command_id"] = cmd_id
        env["correlationid"] = cmd_id
        env["delivery"] = "single_consumer"
        env["idempotency_key"] = idempotency_key or f"{ce_type}:{cmd_id}"
    return env


def parse_hello_frame(raw: str) -> tuple[str, int]:
    """Validate the Pi websocket handshake and normalize defaults."""
    try:
        hello = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("hello frame must be valid JSON") from exc

    if not isinstance(hello, dict) or hello.get("type") != "hello":
        raise ValueError("expected hello frame")

    session_id = str(hello.get("session_id") or f"tonnybox-{uuid.uuid4().hex[:12]}")
    try:
        sample_rate = int(hello.get("sample_rate", 16_000))
    except (TypeError, ValueError) as exc:
        raise ValueError("sample_rate must be an integer") from exc
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    return session_id, sample_rate


async def publish_json(nc: Any, envelope: dict[str, Any]) -> None:
    await nc.publish(envelope["subject"], json.dumps(envelope).encode("utf-8"))
    log_event(
        "bloodbank_published",
        subject=envelope["subject"],
        type=envelope["type"],
        kind=envelope["kind"],
        correlationid=envelope.get("correlationid"),
        session_id=envelope.get("data", {}).get("session_id"),
    )


def pcm_to_wav(pcm: bytes, *, sample_rate: int, channels: int = 1) -> bytes:
    with tempfile.SpooledTemporaryFile() as fp:
        with wave.open(fp, "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        fp.seek(0)
        return fp.read()


def fallback_wav(text: str) -> bytes:
    sample_rate = 16_000
    # Encode a small audible confirmation pattern; real deployments should use voxxy.
    duration_s = min(2.0, max(0.45, len(text) / 40.0))
    sample_count = int(sample_rate * duration_s)
    frames = bytearray()
    for idx in range(sample_count):
        amp = 2800 if (idx // 1600) % 2 == 0 else 1400
        sample = int(amp * ((idx % 160) / 160.0 - 0.5))
        frames.extend(struct.pack("<h", sample))
    return pcm_to_wav(bytes(frames), sample_rate=sample_rate)


def command_argv(command_template: str, **values: str) -> list[str]:
    argv = shlex.split(command_template.format(**{k: shlex.quote(v) for k, v in values.items()}))
    return argv


async def run_command(
    command_template: str,
    *,
    env: dict[str, str] | None = None,
    **values: str,
) -> subprocess.CompletedProcess[str]:
    argv = command_argv(command_template, **values)
    proc_env = None
    if env:
        proc_env = os.environ.copy()
        proc_env.update(env)
    return await asyncio.to_thread(
        subprocess.run,
        argv,
        env=proc_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )


async def run_binary_command(command_template: str, **values: str) -> subprocess.CompletedProcess[bytes]:
    argv = shlex.split(command_template.format(**{k: shlex.quote(v) for k, v in values.items()}))
    return await asyncio.to_thread(
        subprocess.run,
        argv,
        capture_output=True,
        check=False,
        timeout=600,
    )


def parse_transcript_output(stdout: str) -> str:
    raw = stdout.strip()
    if not raw:
        return ""
    maybe_path = transcript_path_from_stdout(raw)
    if maybe_path.exists():
        return read_transcript_markdown(maybe_path)
    return raw


def transcript_path_from_stdout(stdout: str) -> pathlib.Path:
    return pathlib.Path(stdout.strip().splitlines()[-1]).expanduser()


def read_transcript_markdown(path: pathlib.Path) -> str:
    content = path.read_text(encoding="utf-8", errors="replace")
    body = []
    in_body = False
    for line in content.splitlines():
        if line.strip() == "---":
            in_body = True
            continue
        if in_body and line.strip() and not line.startswith("#") and not line.startswith("- **"):
            body.append(line.strip())
    return " ".join(body).strip()


def safe_session_slug(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", session_id).strip("-") or "session"


def transcript_path_for(
    transcript_dir: pathlib.Path,
    session_id: str,
    *,
    now: datetime | None = None,
) -> pathlib.Path:
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return transcript_dir / f"{timestamp:%Y%m%dT%H%M%SZ}-{safe_session_slug(session_id)}.md"


def relocate_transcript_output(stdout: str, destination: pathlib.Path) -> str:
    raw = stdout.strip()
    if not raw:
        return raw

    source = transcript_path_from_stdout(raw)
    if not source.exists():
        return raw

    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        return str(destination)

    shutil.move(str(source), str(destination))
    log_event("transcript_relocated", source=str(source), destination=str(destination))
    return str(destination)


def prompt_from_transcript(transcript: str, *, require_wakeword: bool = True) -> str:
    cleaned = " ".join(str(transcript or "").split()).strip()
    match = WAKEWORD_RE.search(cleaned)
    if match:
        prompt = cleaned[match.end() :].lstrip(" ,.!?:;-")
        if prompt.strip():
            return prompt.strip()
        raise RuntimeError("wakeword transcript did not contain a prompt")
    if require_wakeword:
        raise RuntimeError("transcription did not contain wakeword")
    return cleaned


def normalize_router_payload(payload: Any) -> dict[str, Any]:
    """Accept direct n8n webhook JSON or common item-wrapped shapes."""
    if isinstance(payload, list):
        if not payload:
            raise RuntimeError("router response was an empty list")
        payload = payload[0]
    if isinstance(payload, dict) and isinstance(payload.get("json"), dict):
        payload = payload["json"]
    if isinstance(payload, dict) and isinstance(payload.get("body"), dict):
        payload = payload["body"]
    if not isinstance(payload, dict):
        raise RuntimeError("router response must be a JSON object")
    return payload


def parse_router_response(payload: Any) -> RouterResponse:
    data = normalize_router_payload(payload)
    route = str(data.get("route") or "unknown").strip().lower()
    if route not in ROUTES:
        route = "unknown"
    response_text = str(data.get("response_text") or "").strip()
    if not response_text:
        raise RuntimeError("router response did not include response_text")
    agent_id = str(data.get("agent_id") or f"tonny-{route}").strip()
    agent_display_name = str(data.get("agent_display_name") or agent_id).strip()
    return RouterResponse(
        route=route,
        agent_id=agent_id,
        agent_display_name=agent_display_name,
        response_text=response_text,
        model=str(data.get("model") or "stub").strip(),
        backend=str(data.get("backend") or "stub").strip(),
        execution_id=str(data.get("execution_id") or "").strip(),
    )


def post_json(url: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            response_body = response.read(1024 * 1024)
            status = getattr(response, "status", response.getcode())
    except urllib.error.HTTPError as exc:
        error_body = exc.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"router webhook HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"router webhook unreachable: {exc.reason}") from exc
    if status >= 400:
        raise RuntimeError(f"router webhook HTTP {status}")
    try:
        decoded = json.loads(response_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("router webhook returned invalid JSON") from exc
    return decoded


async def synthesize_wav(text: str, settings: Settings) -> bytes:
    if settings.tts_command:
        try:
            result = await run_binary_command(settings.tts_command, text=text)
            if result.returncode == 0 and result.stdout.startswith(b"RIFF"):
                log_event("tts_synthesized", bytes=len(result.stdout))
                return result.stdout
            log_event(
                "tts_command_failed",
                returncode=result.returncode,
                stderr=result.stderr.decode("utf-8", errors="replace")[:500],
            )
        except Exception:
            log_event("tts_command_exception")
    return fallback_wav(text)


class TonnyBoxGateway:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def connect_nats(self) -> Any:
        if nats is None:
            raise RuntimeError("nats-py is not installed")
        return await nats.connect(self.settings.nats_url, name="tonnybox-server")

    async def upload_audio(self, wav_path: pathlib.Path, session_id: str) -> str:
        key = f"{self.settings.s3_prefix.rstrip('/')}/{datetime.now(timezone.utc):%Y/%m/%d}/{session_id}.wav"
        target = f"{self.settings.s3_alias}/{self.settings.s3_bucket}/{key}"
        if not shutil.which(self.settings.mc_bin):
            raise RuntimeError(f"mc binary not found: {self.settings.mc_bin}")
        proc = await asyncio.to_thread(
            subprocess.run,
            [self.settings.mc_bin, "cp", "--quiet", str(wav_path), target],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"mc upload failed: {proc.stderr.strip()}")
        s3_uri = f"s3://{self.settings.s3_bucket}/{key}"
        log_event("audio_uploaded", session_id=session_id, s3_uri=s3_uri, path=str(wav_path))
        return s3_uri

    async def wait_for_event(
        self,
        nc: Any,
        subject: str,
        predicate: Any,
        timeout_s: float,
    ) -> dict[str, Any] | None:
        future, sub = await self.prepare_event_waiter(nc, subject, predicate)
        try:
            return await self.wait_for_prepared_event(future, sub, timeout_s)
        except asyncio.TimeoutError:
            return None

    async def prepare_event_waiter(self, nc: Any, subject: str, predicate: Any) -> tuple[asyncio.Future, Any]:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        async def on_msg(msg: Any) -> None:
            if future.done():
                return
            try:
                payload = json.loads(msg.data.decode("utf-8"))
            except Exception:
                return
            if predicate(payload):
                future.set_result(payload)

        sub = await nc.subscribe(subject, cb=on_msg)
        return future, sub

    async def wait_for_prepared_event(self, future: asyncio.Future, sub: Any, timeout_s: float) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        finally:
            with contextlib.suppress(Exception):
                await sub.unsubscribe()

    async def transcribe(self, nc: Any, wav_path: pathlib.Path, s3_uri: str, session_id: str) -> TranscriptionResult:
        command_id = str(uuid.uuid4())
        transcription_id = str(uuid.uuid4())
        reply_future, reply_sub = await self.prepare_event_waiter(
            nc,
            "bloodbank.rpy.audio.transcription.start",
            lambda env: env.get("correlationid") == command_id,
        )
        command = build_envelope(
            "bloodbank.audio.transcription.start",
            {
                "id": transcription_id,
                "session_id": session_id,
                "audio_s3_uri": s3_uri,
                "audio_path": str(wav_path),
                "target_agent_id": "transcriber",
            },
            kind="command",
            command_id=command_id,
            idempotency_key=f"audio.transcription.start:{session_id}",
        )
        await publish_json(nc, command)
        log_event(
            "transcription_command_sent",
            session_id=session_id,
            command_id=command_id,
            timeout_s=self.settings.transcribe_reply_timeout_s,
        )

        try:
            reply = await self.wait_for_prepared_event(
                reply_future,
                reply_sub,
                self.settings.transcribe_reply_timeout_s,
            )
        except asyncio.TimeoutError:
            reply = None
        if reply:
            text = str(reply.get("data", {}).get("text", "")).strip()
            if not text:
                raise RuntimeError(f"empty transcription for session {session_id}")
            log_event("transcription_reply_received", session_id=session_id, text=text)
            transcript_path = str(reply.get("data", {}).get("transcript_path") or "").strip()
            return TranscriptionResult(text=text, transcript_path=transcript_path, transcription_id=transcription_id)

        if not self.settings.transcribe_command:
            raise RuntimeError("transcription timed out and TONNYBOX_TRANSCRIBE_COMMAND is empty")

        log_event("transcription_reply_timeout", session_id=session_id)
        transcript_path = transcript_path_for(self.settings.transcript_dir, session_id)
        self.settings.transcript_dir.mkdir(parents=True, exist_ok=True)
        result = await run_command(
            self.settings.transcribe_command,
            audio_path=str(wav_path),
            transcript_dir=str(self.settings.transcript_dir),
            transcript_path=str(transcript_path),
            env={"TRANSCRIPTS": str(self.settings.transcript_dir)},
        )
        if result.returncode != 0:
            raise RuntimeError(f"transcription command failed: {result.stderr.strip()}")
        transcript_stdout = relocate_transcript_output(result.stdout, transcript_path)
        text = parse_transcript_output(transcript_stdout)
        log_event(
            "transcription_local_completed",
            session_id=session_id,
            text=text,
            transcript_path=transcript_stdout.strip(),
        )
        reply_env = build_envelope(
            "bloodbank.audio.transcription.start",
            {
                "id": transcription_id,
                "session_id": session_id,
                "audio_s3_uri": s3_uri,
                "text": text,
            },
            kind="reply",
            correlationid=command_id,
            causationid=command_id,
        )
        await publish_json(nc, reply_env)
        completed = build_envelope(
            "bloodbank.audio.transcription.completed",
            {
                "id": transcription_id,
                "session_id": session_id,
                "audio_s3_uri": s3_uri,
                "text": text,
            },
            ordering_key=f"transcription:{transcription_id}",
            correlationid=command_id,
            causationid=command_id,
        )
        await publish_json(nc, completed)
        if not text.strip():
            raise RuntimeError(f"empty transcription for session {session_id}")
        return TranscriptionResult(
            text=text,
            transcript_path=transcript_stdout.strip(),
            transcription_id=transcription_id,
        )

    async def ask_tonny(self, nc: Any, transcript: str, session_id: str) -> str:
        thread_id = f"tonnybox:{session_id}"
        turn_id = str(uuid.uuid4())
        log_event("tonny_turn_starting", session_id=session_id, turn_id=turn_id, prompt=transcript)
        # Bind the version-free superset, not a narrow filter. This waiter
        # already discriminates on `type` + `turn_id` in the predicate below,
        # so a tighter binding buys nothing and costs everything: it was
        # `bloodbank.evt.v1.>`, which silently matched zero current traffic
        # once the grammar dropped the version token, and a subscription that
        # matches nothing reports no error -- it just never fires. See
        # candystore/ingest.py for the same failure in a live incident.
        response_future, response_sub = await self.prepare_event_waiter(
            nc,
            "bloodbank.evt.>",
            lambda env: (
                (
                    env.get("type") == "bloodbank.conversation.message.appended"
                    and env.get("data", {}).get("turn_id") == turn_id
                    and env.get("data", {}).get("role") == "assistant"
                    and env.get("data", {}).get("source_agent_id") == self.settings.tonny_agent_id
                )
                or (
                    env.get("type") == "bloodbank.agent.invocation.failed"
                    and env.get("data", {}).get("turn_id") == turn_id
                    and env.get("data", {}).get("source_agent_id") == self.settings.tonny_agent_id
                )
            ),
        )
        await publish_json(
            nc,
            build_envelope(
                "bloodbank.conversation.turn.started",
                {"id": turn_id, "thread_id": thread_id, "session_id": session_id, "target_agent_id": self.settings.tonny_agent_id},
                ordering_key=f"turn:{turn_id}",
            ),
        )
        await publish_json(
            nc,
            build_envelope(
                "bloodbank.conversation.message.appended",
                {
                    "id": str(uuid.uuid4()),
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "session_id": session_id,
                    "role": "user",
                    "text": transcript,
                    "target_agent_id": self.settings.tonny_agent_id,
                    "model": self.settings.hermes_model,
                },
                ordering_key=f"turn:{turn_id}",
            ),
        )
        try:
            response = await self.wait_for_prepared_event(
                response_future,
                response_sub,
                self.settings.tonny_reply_timeout_s,
            )
        except asyncio.TimeoutError:
            raise RuntimeError("timed out waiting for Tonny response event")
        if response.get("type") == "bloodbank.agent.invocation.failed":
            error = str(response.get("data", {}).get("error") or "unknown Tonny invocation failure")
            raise RuntimeError(f"Tonny invocation failed: {error}")
        text = str(response.get("data", {}).get("text", "")).strip()
        log_event("tonny_reply_received", session_id=session_id, turn_id=turn_id, text=text)
        return text

    async def route_turn_via_n8n(
        self,
        *,
        session_id: str,
        turn_id: str,
        prompt: str,
        raw_transcript: str,
        s3_uri: str,
        transcript_path: str,
    ) -> RouterResponse:
        if not self.settings.router_webhook_url:
            raise RuntimeError("TONNYBOX_ROUTER_WEBHOOK_URL is not configured")
        payload = {
            "session_id": session_id,
            "turn_id": turn_id,
            "transcript": prompt,
            "raw_transcript": raw_transcript,
            "audio_uri": s3_uri,
            "transcript_path": transcript_path,
            "source": "tonnybox",
            "timestamp": utc_now(),
        }
        log_event(
            "router_turn_starting",
            session_id=session_id,
            turn_id=turn_id,
            url=self.settings.router_webhook_url,
            prompt=prompt,
        )
        response_payload = await asyncio.to_thread(
            post_json,
            self.settings.router_webhook_url,
            payload,
            self.settings.router_timeout_s,
        )
        response = parse_router_response(response_payload)
        log_event(
            "router_reply_received",
            session_id=session_id,
            turn_id=turn_id,
            route=response.route,
            agent_id=response.agent_id,
            backend=response.backend,
            model=response.model,
            execution_id=response.execution_id,
            text=response.response_text,
        )
        return response

    async def send_wav_response(self, websocket: Any, wav_bytes: bytes, *, final: bool, session_id: str) -> None:
        await websocket.send(json.dumps({"type": "response_start", "format": "wav", "final": final}))
        await websocket.send(wav_bytes)
        await websocket.send(json.dumps({"type": "response_end"}))
        log_event(
            "response_sent",
            session_id=session_id,
            bytes=len(wav_bytes),
            final=final,
        )

    async def send_error(self, websocket: Any, *, code: str, message: str, session_id: str | None = None) -> None:
        log_event("gateway_error", session_id=session_id, code=code, message=message)
        with contextlib.suppress(ConnectionClosed, RuntimeError):
            await websocket.send(json.dumps({"type": "error", "code": code, "message": message}))

    async def handle_ws(self, websocket: Any) -> None:
        try:
            session_id, sample_rate = parse_hello_frame(await websocket.recv())
        except ValueError as exc:
            await self.send_error(websocket, code="invalid_hello", message=str(exc))
            return
        await websocket.send(json.dumps({"type": "ready", "session_id": session_id}))
        log_event("session_ready", session_id=session_id, sample_rate=sample_rate)

        pcm = bytearray()
        async for msg in websocket:
            if isinstance(msg, bytes):
                pcm.extend(msg)
                continue
            data = json.loads(msg)
            if data.get("type") == "end_of_input":
                break
            if data.get("type") == "close":
                return

        wav_bytes = pcm_to_wav(bytes(pcm), sample_rate=sample_rate)
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        safe_id = safe_session_slug(session_id)
        wav_path = self.settings.work_dir / f"{safe_id}.wav"
        wav_path.write_bytes(wav_bytes)
        log_event(
            "audio_saved",
            session_id=session_id,
            path=str(wav_path),
            bytes=len(wav_bytes),
            pcm_bytes=len(pcm),
            sample_rate=sample_rate,
        )

        if self.settings.fast_ack_text:
            try:
                ack_wav = await synthesize_wav(self.settings.fast_ack_text, self.settings)
                await self.send_wav_response(websocket, ack_wav, final=False, session_id=session_id)
                log_event("fast_ack_sent", session_id=session_id, text=self.settings.fast_ack_text)
            except Exception as exc:
                log_event("fast_ack_failed", session_id=session_id, error=str(exc))

        nc = None
        try:
            nc = await self.connect_nats()
            s3_uri = await self.upload_audio(wav_path, session_id)
            digest = hashlib.sha256(wav_bytes).hexdigest()
            await publish_json(
                nc,
                build_envelope(
                    "bloodbank.audio.file.received",
                    {"id": digest, "session_id": session_id, "audio_s3_uri": s3_uri, "sha256": digest},
                    ordering_key=f"file:{digest}",
                ),
            )
            try:
                transcription = await self.transcribe(nc, wav_path, s3_uri, session_id)
                prompt = prompt_from_transcript(
                    transcription.text,
                    require_wakeword=self.settings.require_wakeword,
                )
                turn_id = str(uuid.uuid4())
                log_event(
                    "prompt_extracted",
                    session_id=session_id,
                    transcript=transcription.text,
                    prompt=prompt,
                    transcript_path=transcription.transcript_path,
                    turn_id=turn_id,
                )
                if self.settings.router_webhook_url:
                    try:
                        routed = await self.route_turn_via_n8n(
                            session_id=session_id,
                            turn_id=turn_id,
                            prompt=prompt,
                            raw_transcript=transcription.text,
                            s3_uri=s3_uri,
                            transcript_path=transcription.transcript_path,
                        )
                        response_text = routed.response_text
                    except Exception as exc:
                        if not self.settings.router_error_reply:
                            raise
                        response_text = self.settings.router_error_reply
                        log_event(
                            "router_reply_fallback",
                            session_id=session_id,
                            turn_id=turn_id,
                            error=str(exc),
                            text=response_text,
                        )
                else:
                    try:
                        response_text = await self.ask_tonny(nc, prompt, session_id)
                    except Exception as exc:
                        if not self.settings.tonny_error_reply:
                            raise
                        response_text = self.settings.tonny_error_reply
                        log_event(
                            "tonny_reply_fallback",
                            session_id=session_id,
                            turn_id=turn_id,
                            error=str(exc),
                            text=response_text,
                        )
                response_wav = await synthesize_wav(response_text, self.settings)
                await self.send_wav_response(websocket, response_wav, final=True, session_id=session_id)
            except Exception as exc:
                await self.send_error(websocket, code="gateway_failed", message=str(exc), session_id=session_id)
        finally:
            if nc is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(nc.drain(), timeout=2)

    async def serve(self) -> None:
        async def dispatch(websocket: Any) -> None:
            request_path = getattr(websocket, "request", None)
            path = getattr(request_path, "path", self.settings.path)
            if path != self.settings.path:
                await websocket.close(code=1008, reason="unsupported path")
                return
            await self.handle_ws(websocket)

        async with websockets.serve(dispatch, self.settings.host, self.settings.port, max_size=16 * 1024 * 1024):
            await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Validate runtime dependencies and exit")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.check:
        if nats is None:
            raise SystemExit("nats-py is missing")
        if not shutil.which(settings.mc_bin):
            raise SystemExit(f"mc not found: {settings.mc_bin}")
        print("ok")
        return
    asyncio.run(TonnyBoxGateway(settings).serve())


if __name__ == "__main__":
    main()
