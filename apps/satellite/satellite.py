#!/usr/bin/env python3
"""ALSA client for compatible push-to-talk and upstream-wake voice sessions."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import signal
import struct
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake


SAMPLE_RATE = 16_000
FRAME_BYTES = 2_560  # 80 ms of signed 16-bit mono PCM.
MAX_CAPTURE_SECONDS = 15
MAX_PCM_BYTES = SAMPLE_RATE * 2 * MAX_CAPTURE_SECONDS
MAX_WAV_BYTES = 4 * 1024 * 1024
MAX_CONTROL_BYTES = 4_096
DEFAULT_URL = "ws://big-chungus.local:18778/v1/voice"


class ClientError(Exception):
    """A fixed, safe-to-log error code; never gateway text or credentials."""


@dataclass(frozen=True)
class Options:
    url: str = DEFAULT_URL
    capture_device: str = "capture"
    playback_device: str = "playback"
    capture_seconds: float = 6
    connect_timeout: float = 60
    response_timeout: float = 120
    once: bool = False
    input_wav: Path | None = None
    output_wav: Path | None = None
    no_playback: bool = False
    hands_free: bool = False


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    sample_width: int
    data_offset: int
    data_size: int

    @property
    def seconds(self) -> float:
        return self.data_size / (self.sample_rate * self.channels * self.sample_width)


def log(stage: str, **fields: object) -> None:
    print(json.dumps({"stage": stage, **fields}, separators=(",", ":")), flush=True)


def validate_pcm(pcm: bytes) -> None:
    if not pcm or len(pcm) % 2:
        raise ClientError("invalid_pcm_samples")
    if len(pcm) > MAX_PCM_BYTES:
        raise ClientError("capture_too_large")


def pcm_rms(pcm: bytes) -> float:
    validate_pcm(pcm)
    squares = sum(sample * sample for (sample,) in struct.iter_unpack("<h", pcm))
    return round(math.sqrt(squares / (len(pcm) // 2)) / 32768, 6)


def validate_wav(data: bytes, *, capture: bool = False) -> WavInfo:
    """Check RIFF lengths, PCM headers and complete frames before ALSA sees it."""
    if len(data) > MAX_WAV_BYTES:
        raise ClientError("wav_too_large")
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ClientError("invalid_wav_header")
    if struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
        raise ClientError("invalid_wav_length")

    offset = 12
    fmt = None
    audio = None
    while offset < len(data):
        if offset + 8 > len(data):
            raise ClientError("truncated_wav_chunk")
        kind = data[offset : offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        body = offset + 8
        end = body + size
        if end + (size % 2) > len(data):
            raise ClientError("truncated_wav_chunk")
        if kind == b"fmt ":
            if fmt is not None or size < 16:
                raise ClientError("invalid_wav_format")
            fmt = struct.unpack_from("<HHIIHH", data, body)
        elif kind == b"data":
            if audio is not None:
                raise ClientError("duplicate_wav_data")
            audio = (body, size)
        offset = end + (size % 2)

    if fmt is None or audio is None:
        raise ClientError("missing_wav_chunks")
    encoding, channels, rate, byte_rate, block_align, bits = fmt
    if encoding != 1 or channels not in (1, 2) or bits not in (8, 16, 24, 32):
        raise ClientError("unsupported_wav_format")
    width = bits // 8
    if not 8_000 <= rate <= 192_000:
        raise ClientError("unsupported_wav_rate")
    if block_align != channels * width or byte_rate != rate * block_align:
        raise ClientError("invalid_wav_alignment")
    if not audio[1] or audio[1] % block_align:
        raise ClientError("invalid_wav_frames")
    info = WavInfo(rate, channels, width, *audio)
    if capture:
        if (rate, channels, width) != (SAMPLE_RATE, 1, 2):
            raise ClientError("input_wav_must_be_16000hz_mono_pcm16")
        if info.data_size > MAX_PCM_BYTES:
            raise ClientError("capture_too_large")
    return info


def load_input_wav(path: Path) -> bytes:
    # read_bytes() would allocate the entire file before checking its size.
    with path.open("rb") as stream:
        data = stream.read(MAX_WAV_BYTES + 1)
    info = validate_wav(data, capture=True)
    return data[info.data_offset : info.data_offset + info.data_size]


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    """Always reap; escalate if ALSA fails to stop after SIGTERM."""
    async def discard(reader: asyncio.StreamReader | None) -> None:
        if reader is not None:
            while await reader.read(8192):
                pass

    async def reap() -> None:
        # A full stdout pipe can otherwise keep Process.wait() blocked after kill.
        await asyncio.gather(process.wait(), discard(process.stdout), discard(process.stderr))

    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(reap(), timeout=2)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await reap()
    else:
        await reap()


async def read_bounded(reader: asyncio.StreamReader, limit: int) -> bytes:
    data = bytearray()
    while chunk := await reader.read(min(4096, limit + 1 - len(data))):
        if len(data) + len(chunk) > limit:
            raise ClientError("capture_too_large")
        data.extend(chunk)
    return bytes(data)


async def capture_audio(options: Options) -> bytes:
    if not math.isfinite(options.capture_seconds) or not 0 < options.capture_seconds <= MAX_CAPTURE_SECONDS:
        raise ClientError("invalid_capture_duration")
    samples = int(options.capture_seconds * SAMPLE_RATE)
    if samples < 1:
        raise ClientError("invalid_capture_duration")
    process = await asyncio.create_subprocess_exec(
        "arecord", "-q", "-D", options.capture_device,
        "-t", "raw", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1",
        "--samples", str(samples),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(options.capture_seconds + 5):
            assert process.stdout is not None
            pcm = await read_bounded(process.stdout, samples * 2)
            await process.wait()
        if process.returncode:
            raise ClientError("arecord_failed")
        if len(pcm) != samples * 2:
            raise ClientError("short_capture")
        validate_pcm(pcm)
        return pcm
    finally:
        await terminate_process(process)


def control(message: str | bytes) -> dict:
    if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise ClientError("invalid_control_frame")
    try:
        value = json.loads(message)
    except (ValueError, RecursionError) as exc:
        raise ClientError("invalid_control_json") from exc
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ClientError("invalid_control_message")
    if value["type"] == "error":
        # The server's free-form message may contain user text or provider secrets.
        raise ClientError("gateway_error")
    if value["type"] == "close":
        raise ClientError("gateway_closed")
    return value


async def connect_ready(
    options: Options, session_id: str, *, mode: str = "turn"
) -> ClientConnection:
    """Only retry before PCM submission. Keep the capture in RAM for this window."""
    started = time.monotonic()
    deadline = started + options.connect_timeout
    backoff = 1.0
    attempt = 0
    while (remaining := deadline - time.monotonic()) > 0:
        attempt += 1
        websocket = None
        try:
            async with asyncio.timeout(min(10, remaining)):
                websocket = await connect(
                    options.url, proxy=None, compression=None,
                    open_timeout=min(10, remaining), close_timeout=1,
                    max_size=MAX_WAV_BYTES, max_queue=1,
                    ping_interval=20, ping_timeout=20,
                )
                greeting = {
                    "type": "hello", "session_id": session_id,
                    "sample_rate": SAMPLE_RATE, "encoding": "pcm_s16le",
                    "channels": 1, "client": "tonny", "version": "0.1.0",
                }
                if mode != "turn":
                    greeting["mode"] = mode
                await websocket.send(json.dumps(greeting))
                ready = control(await websocket.recv())
                if ready["type"] != "ready" or ready.get("session_id") != session_id:
                    raise ClientError("invalid_ready")
            log("connected", session_id=session_id, attempt=attempt,
                elapsed_seconds=round(time.monotonic() - started, 3))
            return websocket
        except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake) as exc:
            if websocket is not None:
                await websocket.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            delay = min(backoff, remaining)
            log("connect_retry", session_id=session_id, attempt=attempt,
                error_kind=type(exc).__name__, delay_seconds=round(delay, 3))
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, 30)
        except BaseException:
            if websocket is not None:
                await websocket.close()
            raise
    raise ClientError("connect_timeout")


async def exchange(options: Options, pcm: bytes, session_id: str) -> bytes:
    validate_pcm(pcm)
    websocket = await connect_ready(options, session_id)
    try:
        async with asyncio.timeout(options.response_timeout):
            for offset in range(0, len(pcm), FRAME_BYTES):
                await websocket.send(pcm[offset : offset + FRAME_BYTES])
            await websocket.send(json.dumps({"type": "end_of_input"}))
            response = bytearray()
            started = False
            while True:
                message = await websocket.recv()
                if isinstance(message, bytes):
                    if not started:
                        raise ClientError("audio_before_response_start")
                    if len(response) + len(message) > MAX_WAV_BYTES:
                        raise ClientError("response_too_large")
                    response.extend(message)
                    continue
                value = control(message)
                if value["type"] == "response_start":
                    if started or value.get("format") != "wav":
                        raise ClientError("invalid_response_start")
                    started = True
                elif value["type"] == "response_end":
                    if not started:
                        raise ClientError("response_end_before_start")
                    wav = bytes(response)
                    validate_wav(wav)
                    try:
                        await websocket.send(json.dumps({"type": "close"}))
                    except ConnectionClosed:
                        pass  # A gateway may close immediately after response_end.
                    return wav
                else:
                    raise ClientError("unexpected_control_message")
    finally:
        await websocket.close()


async def continuous_exchange(options: Options, session_id: str) -> bytes:
    """Stream live PCM after readiness; never reconnect or replay once submission starts."""
    websocket = await connect_ready(options, session_id, mode="continuous")
    process = await asyncio.create_subprocess_exec(
        "arecord",
        "-q",
        "-D",
        options.capture_device,
        "-t",
        "raw",
        "-f",
        "S16_LE",
        "-r",
        str(SAMPLE_RATE),
        "-c",
        "1",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    submitted = False

    async def send_pcm() -> None:
        nonlocal submitted
        assert process.stdout is not None
        try:
            while True:
                chunk = await process.stdout.readexactly(FRAME_BYTES)
                validate_pcm(chunk)
                submitted = True
                await websocket.send(chunk)
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                raise ClientError("invalid_pcm_samples") from exc
            if process.returncode not in (None, 0):
                raise ClientError("arecord_failed") from exc
            raise ClientError("short_capture") from exc

    sender = asyncio.create_task(send_pcm())
    response = bytearray()
    started = False
    detected = False
    response_deadline: float | None = None

    async def receive_or_capture_failure():
        if started:
            return await websocket.recv()
        incoming = asyncio.create_task(websocket.recv())
        done, _ = await asyncio.wait((incoming, sender), return_when=asyncio.FIRST_COMPLETED)
        if sender in done:
            incoming.cancel()
            await asyncio.gather(incoming, return_exceptions=True)
            return sender.result()
        return incoming.result()

    try:
        while True:
            if response_deadline is not None:
                async with asyncio.timeout_at(response_deadline):
                    message = await receive_or_capture_failure()
            else:
                message = await receive_or_capture_failure()
            if isinstance(message, bytes):
                if not started:
                    raise ClientError("audio_before_response_start")
                if len(response) + len(message) > MAX_WAV_BYTES:
                    raise ClientError("response_too_large")
                response.extend(message)
                continue
            value = control(message)
            if value["type"] == "wake_detected":
                score = value.get("score")
                if (
                    detected
                    or not isinstance(value.get("model"), str)
                    or isinstance(score, bool)
                    or not isinstance(score, (float, int))
                    or not math.isfinite(score)
                    or not 0 <= score <= 1
                ):
                    raise ClientError("invalid_wake_detected")
                detected = True
                response_deadline = time.monotonic() + options.response_timeout
                log(
                    "wake_detected",
                    session_id=session_id,
                    model=value["model"],
                    score=round(float(score), 6),
                )
            elif value["type"] == "response_start":
                if not detected or started or value.get("format") != "wav":
                    raise ClientError("invalid_response_start")
                started = True
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                await terminate_process(process)
            elif value["type"] == "response_end":
                if not started:
                    raise ClientError("response_end_before_start")
                wav = bytes(response)
                validate_wav(wav)
                try:
                    await websocket.send(json.dumps({"type": "close"}))
                except ConnectionClosed:
                    pass
                return wav
            else:
                raise ClientError("unexpected_control_message")
    except (OSError, TimeoutError, ConnectionClosed) as exc:
        # Do not reconnect here. A frame may already have reached the gateway.
        if submitted:
            raise ClientError("continuous_session_lost_after_submission") from exc
        raise
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        await terminate_process(process)
        await websocket.close()


async def play_audio(options: Options, wav: bytes) -> None:
    info = validate_wav(wav)
    process = await asyncio.create_subprocess_exec(
        "aplay", "-q", "-D", options.playback_device, "-t", "wav", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(info.seconds + 10):
            await process.communicate(wav)
        if process.returncode:
            raise ClientError("aplay_failed")
    finally:
        await terminate_process(process)


async def run_turn(options: Options, session_id: str | None = None) -> None:
    if options.no_playback and not (options.once and options.input_wav and options.output_wav):
        raise ClientError("no_playback_requires_once_input_wav_output_wav")
    started = time.monotonic()
    session_id = session_id or str(uuid4())
    log("capture_started", session_id=session_id,
        source="wav" if options.input_wav else "alsa",
        capture_seconds=None if options.input_wav else options.capture_seconds)
    pcm = load_input_wav(options.input_wav) if options.input_wav else await capture_audio(options)
    log("captured", session_id=session_id, bytes=len(pcm), rms=pcm_rms(pcm),
        audio_seconds=round(len(pcm) / (SAMPLE_RATE * 2), 3),
        elapsed_seconds=round(time.monotonic() - started, 3))
    wav = await exchange(options, pcm, session_id)
    info = validate_wav(wav)
    log("response_received", session_id=session_id, bytes=len(wav),
        audio_seconds=round(info.seconds, 3), sample_rate=info.sample_rate,
        elapsed_seconds=round(time.monotonic() - started, 3))
    if options.output_wav:
        options.output_wav.write_bytes(wav)
    if options.no_playback:
        log("response_saved", session_id=session_id, bytes=len(wav), playback="skipped",
            elapsed_seconds=round(time.monotonic() - started, 3))
        return
    # No connection or replay logic follows this point.
    await play_audio(options, wav)
    log("playback_complete", session_id=session_id,
        elapsed_seconds=round(time.monotonic() - started, 3))


async def run_hands_free_turn(options: Options, session_id: str | None = None) -> None:
    if options.input_wav or options.no_playback:
        raise ClientError("hands_free_requires_live_capture_and_playback")
    started = time.monotonic()
    session_id = session_id or str(uuid4())
    log("continuous_capture_started", session_id=session_id, source="alsa")
    wav = await continuous_exchange(options, session_id)
    info = validate_wav(wav)
    log(
        "response_received",
        session_id=session_id,
        bytes=len(wav),
        audio_seconds=round(info.seconds, 3),
        sample_rate=info.sample_rate,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    if options.output_wav:
        options.output_wav.write_bytes(wav)
    await play_audio(options, wav)
    log(
        "playback_complete",
        session_id=session_id,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )


class TriggerGate:
    """One pending trigger at most; signals during a turn are intentionally dropped."""

    def __init__(self) -> None:
        self.pending = asyncio.Event()
        self.busy = False

    def trigger(self) -> bool:
        if self.busy or self.pending.is_set():
            log("trigger_ignored", reason="turn_in_progress")
            return False
        self.pending.set()
        return True


async def serve(options: Options) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    gate = TriggerGate()
    stopping = False

    def stop() -> None:
        nonlocal stopping
        if not stopping:
            stopping = True
            task.cancel()

    loop.add_signal_handler(signal.SIGTERM, stop)
    loop.add_signal_handler(signal.SIGINT, stop)
    handled_signals = [signal.SIGTERM, signal.SIGINT]
    if not options.hands_free:
        loop.add_signal_handler(signal.SIGUSR1, gate.trigger)
        handled_signals.append(signal.SIGUSR1)
    try:
        mode = "hands_free" if options.hands_free else ("once" if options.once else "push_to_talk")
        log("ready", mode=mode, pid=os.getpid())
        while True:
            if not options.once and not options.hands_free:
                await gate.pending.wait()
                gate.pending.clear()
            gate.busy = True
            started = time.monotonic()
            session_id = str(uuid4())
            try:
                if options.hands_free:
                    await run_hands_free_turn(options, session_id)
                else:
                    await run_turn(options, session_id)
            except Exception as exc:
                # Exceptions from networking and ALSA can embed URLs or input.
                log("error", session_id=session_id,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    code=str(exc) if isinstance(exc, ClientError) else type(exc).__name__)
                if options.once:
                    return 1
                if options.hands_free:
                    await asyncio.sleep(1)
            finally:
                gate.busy = False
            if options.once:
                return 0
            log("ready", mode=mode, pid=os.getpid())
    except asyncio.CancelledError:
        log("shutdown")
        return 0
    finally:
        for signum in handled_signals:
            loop.remove_signal_handler(signum)


def parse_options(argv: list[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(
        description="Tonny ALSA satellite with upstream hands-free wake or SIGUSR1 turn mode."
    )
    parser.add_argument("--url", default=os.environ.get("TONNY_GATEWAY_URL", DEFAULT_URL))
    parser.add_argument("--capture-device", default="capture", help="ALSA capture alias (default: capture)")
    parser.add_argument("--playback-device", default="playback", help="ALSA playback alias (default: playback)")
    parser.add_argument("--capture-seconds", type=float, default=6, help="Seconds per turn, up to 15 (default: 6)")
    parser.add_argument("--connect-timeout", type=float, default=60, help="Overall initial connection retry budget (default: 60s)")
    parser.add_argument("--response-timeout", type=float, default=120, help="Overall upload/response budget (default: 120s)")
    parser.add_argument(
        "--hands-free",
        action="store_true",
        help="Continuously stream PCM for wake inference on the gateway",
    )
    parser.add_argument("--once", action="store_true", help="Run one turn immediately, then exit")
    parser.add_argument("--input-wav", type=Path, help="With --once: upload a 16kHz mono PCM16 WAV instead of capturing")
    parser.add_argument("--output-wav", type=Path, help="Save validated reply WAV as runtime test evidence; playback stays enabled by default")
    parser.add_argument("--no-playback", action="store_true", help="Save reply without ALSA; requires --once, --input-wav and --output-wav")
    args = parser.parse_args(argv)
    if not math.isfinite(args.capture_seconds) or not 0.02 <= args.capture_seconds <= MAX_CAPTURE_SECONDS:
        parser.error("--capture-seconds must be between 0.02 and 15")
    for name in ("connect_timeout", "response_timeout"):
        if not math.isfinite(getattr(args, name)) or not 0 < getattr(args, name) <= 300:
            parser.error(f"--{name.replace('_', '-')} must be between 0 and 300 seconds")
    try:
        url = urlsplit(args.url)
        valid_url = url.scheme in ("ws", "wss") and url.hostname and url.port != 0
    except ValueError:
        valid_url = False
    if not valid_url:
        parser.error("--url must be a valid ws:// or wss:// URL")
    if args.no_playback and not (args.once and args.input_wav and args.output_wav):
        parser.error("--no-playback requires --once, --input-wav and --output-wav")
    if args.input_wav and not args.once:
        parser.error("--input-wav requires --once")
    if args.hands_free and (args.input_wav or args.no_playback):
        parser.error("--hands-free requires live capture and playback")
    return Options(**vars(args))


if __name__ == "__main__":
    sys.exit(asyncio.run(serve(parse_options())))
