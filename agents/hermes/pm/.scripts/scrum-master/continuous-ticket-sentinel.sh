#!/usr/bin/env bash
# RETIRED 2026-11-06: the legacy scrum-master sentinel runner. It booted Hermes
# with HERMES_HOME=runtime/ (not a Hermes home: "the gateway exited with status 1")
# and `hermes chat --source cron` (refused by the session store: invalid_params),
# and never handed the PM its board-driver lease. The board pass is the role's
# .scripts/heartbeat.sh: it boots the named profile through the role launcher,
# runs as a `oneshot` session and holds the lease for the pass. Nothing schedules
# either script (Candybar is the single clock); this stub only forwards a manual run.
set -euo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/heartbeat.sh" "$@"
