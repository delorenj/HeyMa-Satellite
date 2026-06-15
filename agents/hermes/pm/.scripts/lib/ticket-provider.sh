# shellcheck shell=bash
# Ticket-provider adapter dispatcher — the single seam between the Scrum Master
# sentinel engine and a concrete ticket system (Linear | Plane | Trello).
#
# The engine NEVER calls a provider directly. It calls `tp <op> [args...]`,
# which dispatches to providers/<provider>.sh. Swapping providers is a one-line
# config change in role.yaml (ticket_provider.name) — no engine edits.
#
# Contract (operations every provider must implement):
#   resolve                       -> JSON {provider, board_id, board_url}
#   active_milestone              -> JSON {id, name, state}
#   list_issues                   -> JSON [ {id,key,title,state,state_type,
#                                            updated_at,assignee,url}, ... ]
#   get_issue <id>                -> JSON {id,key,title,description,acceptance,
#                                          state,state_type,comments:[...]}
#   comment <id> <body>           -> prints comment id
#   transition <id> <normalized>  -> moves issue; normalized in
#                                     backlog|unstarted|started|in_review|completed
#   create_board <name> <id> <d>  -> JSON {board_id, board_url}
#
# Each provider reads its credentials from the environment (see providers/*.sh
# headers) and the board binding from role.yaml under `ticket_provider:`.

# Resolve the provider name: explicit env wins, then role.yaml (self-parsed so
# this works even when _lib.sh / yaml_get is not loaded), then default.
tp_provider_name() {
  if [ -n "${TICKET_PROVIDER:-}" ]; then
    printf '%s\n' "$TICKET_PROVIDER"
    return 0
  fi
  local role_yaml
  role_yaml="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)/role.yaml"
  if [ -f "$role_yaml" ]; then
    local name
    name="$(python3 - "$role_yaml" <<'PY'
import re, sys, pathlib
t = pathlib.Path(sys.argv[1]).read_text()
m = re.search(r'(?ms)^ticket_provider:\s*$(.*?)(?=^\S)', t + "\n\x00")
block = m.group(1) if m else ""
mm = re.search(r'(?m)^\s*name:\s*"?([^"\n]*)"?\s*$', block)
print(mm.group(1).strip() if mm else "")
PY
)"
    [ -n "$name" ] && { printf '%s\n' "$name"; return 0; }
  fi
  printf 'linear\n'
}

# Directory holding provider implementations (sibling of this lib).
tp_providers_dir() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  printf '%s/../providers\n' "$here"
}

# Dispatch one operation to the active provider.
tp() {
  local op="${1:-}"; shift || true
  [ -n "$op" ] || { echo "tp: missing operation" >&2; return 2; }

  local name impl
  name="$(tp_provider_name)"
  impl="$(tp_providers_dir)/${name}.sh"

  if [ ! -f "$impl" ]; then
    echo "tp: unknown ticket provider '$name' (no $impl)" >&2
    return 2
  fi

  TICKET_PROVIDER="$name" sh "$impl" "$op" "$@"
}

# Normalized states the engine reasons in. Adapters map these to provider terms.
TP_STATES="backlog unstarted started in_review completed"

tp_is_valid_state() {
  case " $TP_STATES " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}
