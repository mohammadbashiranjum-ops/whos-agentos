"""
Workspace Read-Token Check (import side effect)
===============================================

Importing this module runs `app.token_guard.enforce_clean_token_env()` once.
`app/main.py` imports it before anything else that builds state (agents,
database handles), so a process that still carries a
`WHOS_WORKSPACE_READ_TOKEN_*` variable exits non-zero before the app exists.
Python caches the import, so the check runs once per process.
"""

from app.token_guard import enforce_clean_token_env

enforce_clean_token_env()
