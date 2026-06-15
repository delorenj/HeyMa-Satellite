#!/usr/bin/env bash
# Resolve or create this agent's ticket board for the selected provider.
# Generalizes the old 40-plane.sh across linear | plane | trello via the
# adapter contract in .scripts/lib/ticket-provider.sh.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env
# shellcheck source=lib/ticket-provider.sh
source "$(dirname "$0")/lib/ticket-provider.sh"

already_done 42-ticket-provider && { log "[42] ticket provider already set up — skipping"; exit 0; }

PROVIDER="$(yaml_get ticket_provider.name)"; PROVIDER="${PROVIDER:-plane}"
log "[42] ticket provider: $PROVIDER"

# 5-char identifier: <repo[0..2]><role[0..1]> uppercased (same scheme as before).
RAW=$(printf '%s%s' "${REPO:0:3}" "${ROLE:0:2}" | tr -cd '[:alnum:]' | tr '[:lower:]' '[:upper:]')
while (( ${#RAW} < 3 )); do RAW="${RAW}X"; done
IDENT="${RAW:0:5}"
NAME="${DISPLAY_NAME//-/ }"
DESC="Hermes agent board for $AGENT_ID"

record_binding() {
  # record_binding <board_id> <board_url>
  [ -n "$1" ] && yaml_set ticket_provider.board_id "$1" || true
  [ -n "$2" ] && yaml_set ticket_provider.board_url "$2" || true
  log "    board_id=$1"
}

case "$PROVIDER" in
  linear)
    if [[ -z "${LINEAR_API_KEY:-}" ]]; then
      warn "[42] LINEAR_API_KEY not set; set role.yaml ticket_provider.team and re-run ./.scripts/42-ticket-provider.sh"
      mark_done 42-ticket-provider; exit 0
    fi
    OUT="$(tp resolve 2>/dev/null || true)"
    BID="$(printf '%s' "$OUT" | python3 -c 'import sys,json;
try: print(json.load(sys.stdin).get("board_id",""))
except Exception: print("")')"
    BURL="$(printf '%s' "$OUT" | python3 -c 'import sys,json;
try: print(json.load(sys.stdin).get("board_url",""))
except Exception: print("")')"
    [ -n "$BID" ] && record_binding "$BID" "$BURL" \
      || warn "[42] linear resolve returned no board; set ticket_provider.team in role.yaml"
    ;;

  plane|trello)
    KEYVAR=PLANE_API_KEY; [ "$PROVIDER" = trello ] && KEYVAR=TRELLO_KEY
    if [[ -z "${!KEYVAR:-}" ]]; then
      warn "[42] $KEYVAR not set; skipping board creation. Set creds and re-run ./.scripts/42-ticket-provider.sh"
      mark_done 42-ticket-provider; exit 0
    fi
    OUT="$(tp create_board "$NAME" "$IDENT" "$DESC")" || die "create_board failed for $PROVIDER"
    BID="$(printf '%s' "$OUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("board_id",""))')"
    BURL="$(printf '%s' "$OUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("board_url",""))')"
    record_binding "$BID" "$BURL"
    if [ "$PROVIDER" = plane ]; then
      # Back-compat for 80-registry.sh / 99-summary.sh.
      echo "$BID" > "$ROLE_DIR/.scripts/.plane-project-id"
      yaml_set ticket_provider.project "$BID" || true
      yaml_set plane.identifier "$IDENT" || true
    else
      yaml_set ticket_provider.board "$BID" || true
    fi
    ;;

  *) die "unknown ticket provider: $PROVIDER (expected linear|plane|trello)" ;;
esac

mark_done 42-ticket-provider
