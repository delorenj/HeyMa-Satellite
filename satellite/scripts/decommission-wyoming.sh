#!/usr/bin/env bash
# decommission-wyoming.sh — stop and disable the legacy Wyoming services on tonny.local.
#
# Reversible. Unit files stay in place at /etc/systemd/system/. To re-enable later:
#   ssh tonny.local 'sudo systemctl enable --now wyoming-satellite wyoming-openwakeword'
#
# This is idempotent. Re-running on already-disabled units is a no-op.

set -euo pipefail

PI_HOST="${PI_HOST:-tonny.local}"
UNITS=(wyoming-satellite wyoming-openwakeword)

echo "==> stopping and disabling Wyoming units on ${PI_HOST}"
for unit in "${UNITS[@]}"; do
  echo "    ${unit}"
  ssh "${PI_HOST}" "sudo systemctl stop ${unit}.service 2>/dev/null || true"
  ssh "${PI_HOST}" "sudo systemctl disable ${unit}.service 2>/dev/null || true"
done

echo "==> verifying state"
for unit in "${UNITS[@]}"; do
  STATE=$(ssh "${PI_HOST}" "systemctl is-enabled ${unit}.service 2>&1 || true")
  ACTIVE=$(ssh "${PI_HOST}" "systemctl is-active ${unit}.service 2>&1 || true")
  echo "    ${unit}: enabled=${STATE} active=${ACTIVE}"
done

echo "==> done. Unit files preserved at /etc/systemd/system/{wyoming-satellite,wyoming-openwakeword}.service"
