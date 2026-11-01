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

# Workspace materialiser call and token scrub live in scripts/workspace_boot.sh, shared with the
# PATH shim scripts/shim/uvicorn (see that file). It leaves `_envu` for the exec below.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/workspace_boot.sh
source "${SCRIPT_DIR}/workspace_boot.sh"
unset SCRIPT_DIR

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
