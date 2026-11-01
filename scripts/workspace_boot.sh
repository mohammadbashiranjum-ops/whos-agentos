#!/bin/bash
# Shared workspace boot (WHOS PEE phase 1, revision 3). SOURCED, never executed, by
# scripts/entrypoint.sh and by the PATH shim scripts/shim/uvicorn, so the materialiser call and the
# token scrub run however the service is started: Railway runs a startCommand INSTEAD of the
# Dockerfile ENTRYPOINT, and a service-level override may be a bare `uvicorn ...`.
#
# After it returns, `_envu` is empty or `(env -u NAME ...)`; the caller execs
# `"${_envu[@]}" <command> "$@"`. Idempotent: the first runner exports WHOS_WORKSPACE_BOOT_DONE=1
# (a marker, no secret) and a later runner (entrypoint -> shim) does not materialise twice. The scrub
# always runs, so a marker set from outside can skip the materialiser but never keeps a token.

# ---------------------------------------------------------------------------
# Pinned workspaces of other repositories (WHOS PEE). Default OFF: with
# WHOS_WORKSPACE_PINS unset the materialiser only prints WORKSPACE_SKIPPED. It never
# fails the container (a bad pin or token must not crash-loop the service); the
# app-start self-check (app/token_guard.py) is the fail-closed half.
# The read tokens are then removed from the environment of the process the caller
# execs, whatever happened above.
# ---------------------------------------------------------------------------
_boot_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${WHOS_WORKSPACE_BOOT_DONE:-}" != "1" ]]; then
    _python="$(command -v python || command -v python3 || true)"
    # Overall deadline for the whole materialiser run (each fetch has its own 60 s bound; the total was
    # unbounded against the 600 s healthcheck timeout). A python-side wait, so it needs no `timeout`
    # binary. On expiry the materialiser is killed, everything it staged or exposed is purged (fail
    # closed, no exposure) and startup continues.
    _deadline="${WHOS_WORKSPACE_BOOT_DEADLINE_SECONDS:-240}"
    case "${_deadline}" in '' | *[!0-9]* | 0) _deadline=240 ;; esac
    if [[ -z "${_python}" ]]; then
        echo "WORKSPACE_REFUSED repo=* reason=materialiser_failed"
    else
        "${_python}" -I -c 'import subprocess, sys
p = subprocess.Popen([sys.executable, "-I", sys.argv[1]])
try:
    sys.exit(p.wait(timeout=int(sys.argv[2])))
except subprocess.TimeoutExpired:
    p.kill()
    p.wait()
    sys.exit(124)' "${_boot_dir}/materialise_workspaces.py" "${_deadline}"
        _rc=$?
        if [[ "${_rc}" -eq 124 ]]; then
            echo "WORKSPACE_REFUSED repo=* reason=deadline"
            "${_python}" -I "${_boot_dir}/materialise_workspaces.py" --purge || true
        elif [[ "${_rc}" -ne 0 ]]; then
            echo "WORKSPACE_REFUSED repo=* reason=materialiser_failed"
        fi
    fi
    export WHOS_WORKSPACE_BOOT_DONE=1
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
unset _name _n _kv _scrubbed _python _boot_dir _deadline _rc
