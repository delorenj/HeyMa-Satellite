#!/usr/bin/env python3
"""Operate the local Compose stack without SSH or systemd."""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
KEYS = ("TONNY_DEEPGRAM_API_KEY", "TONNY_CARTESIA_API_KEY", "TONNY_LLM_API_KEY")


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=kwargs.pop("check", True), **kwargs)


class Stack:
    def __init__(self, project: str = "tonny-local", **environment: str):
        self.env = {**os.environ, "COMPOSE_DISABLE_ENV_FILE": "1", **environment}
        self.command = [
            "docker",
            "compose",
            "--env-file",
            os.devnull,
            "--project-name",
            project,
            "-f",
            str(ROOT / "compose.local.yaml"),
        ]

    def compose(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        return run(self.command + list(args), cwd=ROOT, env=self.env, **kwargs)

    def url(self) -> str:
        address = self.compose(
            "port", "gateway", "18778", capture_output=True, text=True
        ).stdout.strip()
        if not address:
            raise RuntimeError("The local gateway is not running. Run tonny:local:up first.")
        return "http://" + address

    def wav(self, source: Path, output: Path) -> None:
        source, output = source.expanduser().resolve(), output.expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"Input WAV does not exist: {source}")
        if source == output:
            raise ValueError("Input and output paths must be different.")
        output.parent.mkdir(parents=True, exist_ok=True)
        self.url()  # Avoid creating a dependency gateway with different provider settings.
        self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--volume",
            f"{source}:/input/request.wav:ro",
            "--volume",
            f"{output.parent}:/output",
            "satellite",
            "--once",
            "--input-wav",
            "/input/request.wav",
            "--output-wav",
            "/output/" + output.name,
            "--no-playback",
        )
        print(f"Reply saved: {output}", flush=True)


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=4) as response:
        return json.load(response)


def pcm_wave(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    return output.getvalue()


def wait_json(url: str, predicate, timeout: float = 45) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = get_json(url)
            if predicate(data):
                return data
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {url}")


def check() -> None:
    project = "tonny-local-check-" + uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="tonny-local-check-") as directory:
        scratch = Path(directory)
        source = scratch / "src"
        shutil.copytree(
            ROOT / "apps/voice/src",
            source,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        stack = Stack(
            project,
            TONNY_MODE="loopback",
            TONNY_LOCAL_PORT="0",
            TONNY_VOICE_SOURCE=str(source),
            **dict.fromkeys(KEYS, ""),
        )
        try:
            stack.compose("build", "gateway", "satellite", "browser-check")
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--env",
                    "TONNY_MODE=loopback",
                    f"{project}-gateway",
                    "python",
                    "-c",
                    "import asyncio,io,wave; from tonny_voice.app import create_app; "
                    "app=create_app(); pcm=b'\\x01\\x00'*16000; "
                    "reply=asyncio.run(app.state.engine.process(pcm)); "
                    "wav=wave.open(io.BytesIO(reply.wav)); "
                    "assert wav.readframes(wav.getnframes())==pcm; "
                    "print('PASS: image loopback works with networking disabled.')",
                ]
            )
            stack.compose("up", "-d", "--wait", "--wait-timeout", "180", "gateway")
            base = stack.url()
            health = get_json(base + "/healthz")
            assert health["mode"] == "loopback", health
            assert not any(health["configured"].values()), health
            pcm = b"".join(
                struct.pack("<h", round(4000 * math.sin(2 * math.pi * 440 * i / 16000)))
                for i in range(16000)
            )
            request, reply = scratch / "request.wav", scratch / "reply.wav"
            request.write_bytes(pcm_wave(pcm))
            stack.wav(request, reply)
            with wave.open(str(reply), "rb") as wav:
                assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (
                    1,
                    2,
                    16000,
                )
                assert wav.readframes(wav.getnframes()) == pcm
            health = get_json(base + "/healthz")
            assert health["evidence_since_start"]["responses_sent"] == 1
            assert all(
                health["evidence_since_start"][k] == 0
                for k in ("stt_turns", "llm_turns", "tts_turns")
            )
            print(
                "PASS: container satellite preserves PCM; provider activity is zero.",
                flush=True,
            )
            stack.compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--entrypoint",
                "python",
                "satellite",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            )
            stack.compose("run", "--rm", "--no-deps", "-T", "browser-check")

            # Edit only this check's temporary source tree to prove an actual worker reload.
            probe = uuid4().hex
            app_source = source / "tonny_voice/app.py"
            with app_source.open("a") as file:
                file.write(
                    "\n_original_create_app = create_app\n"
                    "def create_app(*args, **kwargs):\n"
                    "    app = _original_create_app(*args, **kwargs)\n"
                    "    @app.get('/__reload_probe')\n"
                    "    async def reload_probe():\n"
                    f"        return {{'probe': '{probe}'}}\n"
                    "    return app\n"
                )
            wait_json(base + "/__reload_probe", lambda data: data.get("probe") == probe)
            assert get_json(base + "/healthz")["evidence_since_start"]["responses_sent"] == 0
            stack.wav(request, reply)
            print(
                "PASS: source reload restarts the worker and the next turn succeeds.",
                flush=True,
            )
        except BaseException:
            stack.compose("logs", "--no-color", "--tail", "60", "gateway", check=False)
            raise
        finally:
            stack.compose("down", "--remove-orphans", "--volumes")
    print("Local container checks passed; isolated test stack removed.", flush=True)


def up(live: bool) -> None:
    if live and not all(os.environ.get(key) for key in KEYS):
        if os.environ.get("TONNY_OP_LAUNCHED"):
            raise RuntimeError("1Password did not supply all three voice provider credentials.")
        if not shutil.which("op"):
            raise RuntimeError(
                "Sign in to 1Password CLI, or supply the three TONNY_* API keys in the environment."
            )
        result = subprocess.run(
            [
                "op",
                "run",
                "--env-file",
                str(ROOT / "deploy/tonny/voice.env.op"),
                "--",
                sys.executable,
                str(Path(__file__).resolve()),
                "up",
                "--live",
            ],
            env={**os.environ, "TONNY_OP_LAUNCHED": "1"},
            check=False,
        )
        raise SystemExit(result.returncode)
    overrides = {} if live else dict.fromkeys(KEYS, "")
    stack = Stack(TONNY_MODE="live" if live else "loopback", **overrides)
    stack.compose("build", "gateway", "satellite")
    stack.compose("up", "-d", "--wait", "--wait-timeout", "180", "gateway")
    health = get_json(stack.url() + "/healthz")
    if live and not all(health["configured"].values()):
        raise RuntimeError(
            "The local gateway is running but its voice providers are not configured."
        )
    print(
        f"Tonny {health['mode']}: {stack.url().replace('127.0.0.1', 'localhost')}",
        flush=True,
    )
    print(
        "Python edits reload automatically; refresh the browser for web asset edits.",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("up", help="Build/start offline mode; --live uses voice providers")
    start.add_argument("--live", action="store_true")
    wav = commands.add_parser("wav", help="Send a WAV through the running local gateway")
    wav.add_argument("--input", type=Path, required=True)
    wav.add_argument("--output", type=Path, required=True)
    commands.add_parser("check", help="Run isolated offline container/browser/reload checks")
    commands.add_parser("status", help="Show containers and health without exposing credentials")
    logs = commands.add_parser("logs", help="Show recent gateway logs")
    logs.add_argument("--follow", action="store_true")
    commands.add_parser("down", help="Remove only the local development stack")
    args = parser.parse_args(argv)
    try:
        if args.command == "up":
            up(args.live)
        elif args.command == "check":
            check()
        else:
            stack = Stack()
            if args.command == "wav":
                stack.wav(args.input, args.output)
            elif args.command == "status":
                stack.compose("ps")
                print(json.dumps(get_json(stack.url() + "/healthz"), indent=2))
            elif args.command == "logs":
                stack.compose(
                    "logs",
                    "--tail",
                    "100",
                    *(["--follow"] if args.follow else []),
                    "gateway",
                )
            elif args.command == "down":
                stack.compose("down", "--remove-orphans")
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Tonny local: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
