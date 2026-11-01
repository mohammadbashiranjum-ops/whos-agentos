#!/bin/bash

############################################################################
#
#    Agno Container Entrypoint
#
############################################################################

# Colors
ORANGE='\033[38;5;208m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${ORANGE}"
cat << 'BANNER'
     █████╗  ██████╗ ███╗   ██╗ ██████╗
    ██╔══██╗██╔════╝ ████╗  ██║██╔═══██╗
    ███████║██║  ███╗██╔██╗ ██║██║   ██║
    ██╔══██║██║   ██║██║╚██╗██║██║   ██║
    ██║  ██║╚██████╔╝██║ ╚████║╚██████╔╝
    ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝ ╚═════╝
BANNER
echo -e "${NC}"

if [[ "${WAIT_FOR_DB:-}" = true || "${WAIT_FOR_DB:-}" = True ]]; then
    echo -e "    ${DIM}Waiting for database at ${DB_HOST}:${DB_PORT}...${NC}"
    dockerize -wait "tcp://${DB_HOST}:${DB_PORT}" -timeout 300s
    echo -e "    ${BOLD}Database ready.${NC}"
    echo ""
fi

# ---------------------------------------------------------------------------
# Pinned workspaces of other repositories (WHOS PEE). Default OFF: with
# WHOS_WORKSPACE_PINS unset the script only prints WORKSPACE_SKIPPED. It never
# fails the container (a bad pin or token must not crash-loop the service); the
# app-start self-check (app/token_guard.py) is the fail-closed half.
# The read tokens are then removed from THIS shell's environment, whatever
# happened above, so the process that `exec "$@"` starts never inherits them.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_python="$(command -v python || command -v python3 || true)"
if [[ -z "${_python}" ]] || ! "${_python}" -I "${SCRIPT_DIR}/materialise_workspaces.py"; then
    echo "WORKSPACE_REFUSED repo=* reason=materialiser_failed"
fi
_scrubbed=0
for _name in $(compgen -e | grep -iE '^WHOS_WORKSPACE_READ_TOKEN_' || true); do
    unset "${_name}"
    _scrubbed=$((_scrubbed + 1))
done
# A name with a character a shell identifier cannot hold (for example ``..._TOKEN_A.B``) cannot be `unset`
# but is still passed to children. Whatever survived the loop above is stripped from the exec'd process
# with ``env -u`` (names are read NUL-delimited from ``env -0``; values never leave the read variable).
_envu=()
while IFS= read -r -d '' _kv; do
    _n="${_kv%%=*}"
    case "${_n^^}" in
        WHOS_WORKSPACE_READ_TOKEN_*) _envu+=(-u "${_n}"); _scrubbed=$((_scrubbed + 1)) ;;
    esac
done < <(env -0)
if [[ "${_scrubbed}" -gt 0 ]]; then
    echo "TOKEN_SCRUBBED scope=entrypoint removed=${_scrubbed}"
fi
if [[ "${#_envu[@]}" -gt 0 ]]; then
    _envu=(env "${_envu[@]}")
fi
unset _name _n _kv _scrubbed _python SCRIPT_DIR

case "$1" in
    chill)
        echo -e "    ${DIM}Mode: chill${NC}"
        echo -e "    ${BOLD}Container running.${NC}"
        echo ""
        while true; do sleep 18000; done
        ;;
    *)
        # `$*` for the echo, `"$@"` for the exec — the echo wants one display string,
        # and only "$@" keeps an argument that contains spaces a single argument.
        echo -e "    ${DIM}> $*${NC}"
        echo ""
        exec "${_envu[@]}" "$@"
        ;;
esac
