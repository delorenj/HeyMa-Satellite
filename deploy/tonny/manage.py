#!/usr/bin/env python3
"""Install committed Tonny source and operate the Pi without secret files."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATHS = ["apps/voice", "apps/satellite", "deploy/tonny"]
SSH = [
    "ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=8",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "UpdateHostKeys=no",
    "tonny",
]


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kwargs)


def remote(script: str, *, timeout: int = 180) -> str:
    return run(
        SSH + ["bash -se"],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
    ).stdout


def health() -> dict:
    with urllib.request.urlopen(
        "http://127.0.0.1:18778/healthz", timeout=5
    ) as response:
        return json.load(response)


def voice_snapshot() -> dict:
    path = Path.home() / ".config/systemd/user/tonny-voice.service"
    return {
        "unit": path.read_text() if path.exists() else None,
        "active": subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "tonny-voice.service"],
            check=False,
        ).returncode
        == 0,
        "enabled": subprocess.run(
            ["systemctl", "--user", "is-enabled", "--quiet", "tonny-voice.service"],
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0,
    }


def restore_voice(snapshot: dict) -> None:
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", "tonny-voice.service"], check=False
    )
    path = Path.home() / ".config/systemd/user/tonny-voice.service"
    if snapshot["unit"] is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(snapshot["unit"])
    run(["systemctl", "--user", "daemon-reload"])
    if snapshot["enabled"]:
        run(["systemctl", "--user", "enable", "tonny-voice.service"])
    if snapshot["active"]:
        run(["systemctl", "--user", "start", "tonny-voice.service"])


def satellite_snapshot() -> dict:
    return json.loads(
        remote(
            """python3 - <<'PY'
import json, subprocess
from pathlib import Path
p = Path('/etc/systemd/system/tonny-satellite.service')
state = {'unit': p.read_text() if p.exists() else None, 'services': {}}
for name in ['tonny-satellite.service', 'heyma.service']:
    state['services'][name] = {action: subprocess.run(['systemctl', action, '--quiet', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0 for action in ['is-active', 'is-enabled']}
print(json.dumps(state))
PY
""",
            timeout=15,
        )
    )


def restore_satellite(snapshot: dict) -> None:
    remote(
        "sudo -n python3 - <<'PY'\n"
        + "state = "
        + repr(snapshot)
        + "\n"
        + """import subprocess
from pathlib import Path
subprocess.run(['systemctl', 'disable', '--now', 'tonny-satellite.service'], check=False)
p = Path('/etc/systemd/system/tonny-satellite.service')
if state['unit'] is None:
    p.unlink(missing_ok=True)
else:
    p.write_text(state['unit'])
subprocess.run(['systemctl', 'daemon-reload'], check=True)
for name, previous in state['services'].items():
    exists = subprocess.run(['systemctl', 'cat', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if exists:
        subprocess.run(['systemctl', 'enable' if previous['is-enabled'] else 'disable', name], check=True)
        subprocess.run(['systemctl', 'start' if previous['is-active'] else 'stop', name], check=True)
PY
""",
        timeout=30,
    )


def deploy() -> None:
    # Deploy Git's content, and refuse to hide any pending changes to that content.
    run(["git", "diff", "--exit-code", "HEAD", "--", *SOURCE_PATHS], cwd=ROOT)
    untracked = run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *SOURCE_PATHS],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout
    if untracked.strip():
        raise SystemExit("Commit the new Tonny source before deploying:\n" + untracked)
    revision = run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    release = Path.home() / ".local/share/tonny/releases" / revision
    release.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as archive:
        run(["git", "archive", revision, *SOURCE_PATHS], cwd=ROOT, stdout=archive)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as packed:
            packed.extractall(release, filter="data")
    (release / "REVISION").write_text(revision + "\n")
    run(
        ["uv", "sync", "--frozen", "--no-dev", "--project", str(release / "apps/voice")]
    )
    previous_voice = voice_snapshot()
    previous_satellite = satellite_snapshot()
    try:
        activate(release, revision)
    except (Exception, KeyboardInterrupt, SystemExit):
        try:
            restore_satellite(previous_satellite)
        finally:
            restore_voice(previous_voice)
        raise


def activate(release: Path, revision: str) -> None:
    tools = [
        str(Path(shutil.which(tool) or tool).resolve().parent) for tool in ["uv", "op"]
    ]
    tool_path = ":".join(dict.fromkeys(tools + ["/usr/local/bin", "/usr/bin", "/bin"]))
    unit = (release / "deploy/tonny/voice.service.in").read_text()
    unit = unit.replace("@RELEASE@", str(release)).replace("@TOOL_PATH@", tool_path)
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "tonny-voice.service").write_text(unit)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "tonny-voice.service"])
    run(["systemctl", "--user", "restart", "tonny-voice.service"])
    for attempt in range(30):
        try:
            current = health()
            if not all(current.get("configured", {}).values()):
                raise ValueError("Gateway credentials are not configured")
            if current.get("commit", current.get("revision")) != revision:
                raise ValueError("Gateway has not loaded this revision yet")
            print(json.dumps(current, sort_keys=True))
            break
        except (OSError, ValueError):
            if attempt == 29:
                raise SystemExit(
                    "Voice gateway did not become healthy; inspect journalctl --user -u tonny-voice"
                )
            time.sleep(1)

    pi_release = "/opt/tonny/releases/" + revision
    quote_release = shlex.quote(pi_release)
    remote(
        f"sudo -n mkdir -p {quote_release}\nsudo -n chown delorenj:audio {quote_release}\n"
    )
    with tempfile.TemporaryFile() as archive:
        run(
            [
                "git",
                "archive",
                revision,
                "apps/satellite",
                "deploy/tonny/satellite.service.in",
            ],
            cwd=ROOT,
            stdout=archive,
        )
        archive.seek(0)
        run(SSH + [f"tar -xf - -C {quote_release}"], stdin=archive)
    satellite_hash = hashlib.sha256(
        (release / "apps/satellite/satellite.py").read_bytes()
    ).hexdigest()
    script = f"""set -euo pipefail
cd {quote_release}
printf '%s  %s\\n' {shlex.quote(satellite_hash)} apps/satellite/satellite.py | sha256sum --check
printf '%s\\n' {shlex.quote(revision)} > REVISION
python3 -m venv venv
venv/bin/python -m pip install --disable-pip-version-check -r apps/satellite/requirements.txt
python3 -c 'import urllib.request; print(urllib.request.urlopen("http://big-chungus.local:18778/healthz", timeout=8).status)'
sed 's|@RELEASE@|{pi_release}|g' deploy/tonny/satellite.service.in | sudo -n tee /etc/systemd/system/tonny-satellite.service >/dev/null
sudo -n systemctl daemon-reload
sudo -n systemctl disable --now heyma.service
sudo -n systemctl enable tonny-satellite.service
sudo -n systemctl restart tonny-satellite.service
sleep 2
sudo -n systemctl is-active --quiet tonny-satellite.service
"""
    try:
        print(remote(script, timeout=300))
    except subprocess.CalledProcessError as exc:
        print(exc.stdout or "", end="")
        print(exc.stderr or "", end="")
        raise
    print("Installed voice gateway and satellite from " + revision)
    status()


def status() -> None:
    print("Gateway:", json.dumps(health(), indent=2, sort_keys=True))
    print(
        remote(
            "systemctl show tonny-satellite.service -p ActiveState -p MainPID -p ExecStart\n"
            "journalctl -u tonny-satellite.service -n 8 --no-pager\n",
            timeout=20,
        )
    )


def talk() -> None:
    remote(
        "sudo -n systemctl kill --kill-whom=main --signal=SIGUSR1 tonny-satellite.service\n",
        timeout=15,
    )
    print(
        "Tonny is recording for 6 seconds. Speak now; the reply will play on its speaker."
    )


def check() -> None:
    run(
        ["uv", "run", "--project", str(ROOT / "apps/voice"), "--locked", "pytest"],
        cwd=ROOT / "apps/voice",
    )
    run(
        [
            "uv",
            "run",
            "--no-project",
            "--python",
            "3.13",
            "--with-requirements",
            str(ROOT / "apps/satellite/requirements.txt"),
            "python",
            "-m",
            "unittest",
            "discover",
            "-s",
            "apps/satellite/tests",
            "-v",
        ],
        cwd=ROOT,
    )
    run(["python3", "-m", "py_compile", str(Path(__file__))])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["deploy", "status", "talk", "check"])
    args = parser.parse_args()
    {"deploy": deploy, "status": status, "talk": talk, "check": check}[args.action]()


if __name__ == "__main__":
    main()
