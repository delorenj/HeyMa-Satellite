#!/usr/bin/env bash
# Ensure a single shared fleet source-of-truth file exists.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 05-fleet-env && { log "[05] fleet env already checked — skipping"; exit 0; }

mkdir -p "$(dirname "$FLEET_ENV")"

if [[ ! -f "$FLEET_ENV" ]]; then
  log "[05] creating fleet source-of-truth: $FLEET_ENV"
  cat > "$FLEET_ENV" <<EOF
# Hermes fleet source of truth.
# All generated wrappers and provisioning scripts read this file.
HERMES_FLEET_BIN=${HERMES_BIN}
HERMES_FLEET_REPO=${HERMES_AGENT_REPO}
HERMES_FLEET_REGISTRY_FILE=${REGISTRY_FILE}
EOF
  chmod 600 "$FLEET_ENV"
else
  log "[05] fleet env exists: $FLEET_ENV"
fi

mark_done 05-fleet-env
