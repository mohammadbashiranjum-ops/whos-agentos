"""
Workspace Read-Token Guard
==========================

WHOS PEE phase 1. The read tokens for the pinned workspaces
(``WHOS_WORKSPACE_READ_TOKEN_*``) are meant to be read once by
``scripts/materialise_workspaces.py`` from ``scripts/entrypoint.sh`` and removed
before the application starts. This guard is the observable check that the
removal really happened in the process that serves requests.

``TOKEN_ENV_CLEAN`` is logged when no such variable is in the environment.
Otherwise ``TOKEN_ENV_LEAK`` is logged (names only, never values), the variables
are deleted from ``os.environ`` and the process exits non-zero, so the pinned
workspaces are never served from a process that carries a read token. That can
happen when the platform's start command bypasses the container ENTRYPOINT.
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping

TOKEN_PREFIX = "WHOS_WORKSPACE_READ_TOKEN_"
LEAK_EXIT_CODE = 78  # EX_CONFIG


def leaked_token_names(environ: MutableMapping[str, str]) -> list[str]:
    return sorted(name for name in environ if name.upper().startswith(TOKEN_PREFIX))


def enforce_clean_token_env(environ: MutableMapping[str, str] | None = None, exit_fn=sys.exit) -> bool:
    """Return True when clean. On a leak: log, delete the variables, call ``exit_fn``."""

    env = os.environ if environ is None else environ
    names = leaked_token_names(env)
    if not names:
        print("TOKEN_ENV_CLEAN", flush=True)
        return True
    for name in names:
        del env[name]
    print(f"TOKEN_ENV_LEAK count={len(names)} names={','.join(names)} action=deleted_and_exit", flush=True)
    exit_fn(LEAK_EXIT_CODE)
    return False
