#!/usr/bin/env bash
# deploy.sh — cross-compile heyma for aarch64 and install on tonny.local.
#
# Idempotent. Safe to re-run after every change.
#
# Prereqs (one-time on big-chungus):
#   cargo install cross --git https://github.com/cross-rs/cross
#   rustup target add aarch64-unknown-linux-gnu
#   ssh tonny.local 'sudo install -d /usr/local/bin /etc/systemd/system'
#   ssh tonny.local 'test -f /etc/heyma.env || sudo install -m 600 -o delorenj /dev/null /etc/heyma.env'
#
# Then populate /etc/heyma.env on the Pi with the HEYMA_* env vars before first start.
# Example (minimum):
#   HEYMA_GATEWAY_URL=ws://192.168.1.12:8778/v1/voice
#   HEYMA_WAKE_MODEL_PATH=/home/delorenj/custom_wakewords/hey_tonny.onnx
#   RUST_LOG=heyma=info

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CRATE_DIR="${REPO_ROOT}/heyma-satellite"
TARGET="aarch64-unknown-linux-gnu"
PI_HOST="${PI_HOST:-tonny.local}"
PI_BIN="/usr/local/bin/heyma"
PI_UNIT="/etc/systemd/system/heyma.service"
UNIT_SRC="${REPO_ROOT}/satellite/systemd/heyma.service"

echo "==> cross-compile heyma for ${TARGET}"
cd "${CRATE_DIR}"

# Prefer `cross` because cpal's ALSA backend needs a target sysroot.
# Fallback to plain cargo if `cross` is unavailable but a sysroot is configured.
if command -v cross >/dev/null 2>&1; then
  cross build --release --target "${TARGET}" --features real-wake
else
  echo "    cross not found; trying plain cargo (requires aarch64 sysroot to be configured)" >&2
  cargo build --release --target "${TARGET}" --features real-wake
fi

BINARY="${CRATE_DIR}/target/${TARGET}/release/heyma"
test -x "${BINARY}" || { echo "build did not produce ${BINARY}" >&2; exit 1; }
test -s "${BINARY}" || { echo "binary at ${BINARY} is zero bytes; aborting deploy" >&2; exit 1; }

echo "==> rsync binary to ${PI_HOST}:${PI_BIN}"
rsync -avz --progress "${BINARY}" "${PI_HOST}:/tmp/heyma.new"
ssh "${PI_HOST}" "sudo install -m 0755 /tmp/heyma.new ${PI_BIN}"
ssh "${PI_HOST}" "rm -f /tmp/heyma.new"

echo "==> install systemd unit"
scp "${UNIT_SRC}" "${PI_HOST}:/tmp/heyma.service"
ssh "${PI_HOST}" "sudo install -m 0644 /tmp/heyma.service ${PI_UNIT}"
ssh "${PI_HOST}" "rm -f /tmp/heyma.service"

echo "==> reload + restart"
ssh "${PI_HOST}" "sudo systemctl daemon-reload"
ssh "${PI_HOST}" "sudo systemctl enable heyma"
ssh "${PI_HOST}" "sudo systemctl restart heyma"

# Default to one-shot status. Pass --tail to follow logs interactively.
if [[ "${1:-}" == "--tail" ]]; then
  echo "==> tailing journalctl (Ctrl-C to detach, service keeps running)"
  ssh -t "${PI_HOST}" "sudo journalctl -u heyma -f --since '5 seconds ago'"
else
  echo "==> deploy complete; recent logs:"
  ssh "${PI_HOST}" "sudo journalctl -u heyma --since '10 seconds ago' --no-pager"
  echo
  echo "(re-run with --tail to follow logs)"
fi
