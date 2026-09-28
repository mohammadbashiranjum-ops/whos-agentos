"""
Regression tests for scripts/bootstrap_whos_sa.py
=================================================

Hermetic: the real script is loaded with stand-ins for agno's service-account
module and the app's database, so these run anywhere with the standard library
and drive the script's own main() end to end. What matters is the decision it
takes for a given account row -- READY (exit 0) or refuse (exit 1) -- and that a
refusal never creates or mutates anything.

Run: python -m unittest discover -s scripts -p "test_*.py"
"""

import contextlib
import hashlib
import importlib.util
import io
import os
import sys
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

SCRIPT = Path(__file__).with_name("bootstrap_whos_sa.py")
TOKEN = "agno_pat_" + "a" * 64
SCOPES = [
    "config:read",
    "sessions:read",
    "agents:platform-engineer:read",
    "agents:platform-engineer:run",
]
DAY = 24 * 60 * 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class FakeDb:
    """The three service-account calls the script makes, over a list of rows."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.created: list[dict[str, Any]] = []

    def get_service_account_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        return next((r for r in self.rows if r["token_hash"] == token_hash), None)

    def get_service_account_by_name(self, name: str, include_revoked: bool = False) -> dict[str, Any] | None:
        for r in self.rows:
            if r["name"] == name and (include_revoked or r.get("revoked_at") is None):
                return r
        return None

    def create_service_account(self, data: dict[str, Any]) -> None:
        self.created.append(data)
        self.rows.append(data)


def load_script(db: FakeDb) -> types.ModuleType:
    """Import the real script with agno's service-account API and db stubbed."""

    class ServiceAccount:
        def __init__(self, **kwargs: Any):
            self.__dict__.update(kwargs)

        def to_dict(self) -> dict[str, Any]:
            return dict(self.__dict__)

    service_accounts_os = types.ModuleType("agno.os.service_accounts")
    service_accounts_os.TOKEN_DISPLAY_PREFIX_LENGTH = 16  # type: ignore[attr-defined]
    service_accounts_os.hash_token = _hash  # type: ignore[attr-defined]
    service_accounts_schema = types.ModuleType("agno.db.schemas.service_accounts")
    service_accounts_schema.ServiceAccount = ServiceAccount  # type: ignore[attr-defined]
    db_module = types.ModuleType("db")
    db_module.get_postgres_db = lambda: db  # type: ignore[attr-defined]

    stubs = {
        "agno": types.ModuleType("agno"),
        "agno.os": types.ModuleType("agno.os"),
        "agno.os.service_accounts": service_accounts_os,
        "agno.db": types.ModuleType("agno.db"),
        "agno.db.schemas": types.ModuleType("agno.db.schemas"),
        "agno.db.schemas.service_accounts": service_accounts_schema,
        "db": db_module,
    }
    spec = importlib.util.spec_from_file_location("bootstrap_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


def run_bootstrap(rows: list[dict[str, Any]], token: str = TOKEN) -> tuple[int, str, FakeDb]:
    """Run main() once; return (exit code, combined output, the fake db)."""
    db = FakeDb(rows)
    module = load_script(db)
    out = io.StringIO()
    env = {"WHOS_PEE_AGENTOS_TOKEN": token}
    with (
        mock.patch.dict(os.environ, env, clear=True),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(out),
    ):
        try:
            module.main()
            code = 0
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue(), db


def active_row(expires_at: Any) -> dict[str, Any]:
    return {
        "name": "whos-pee-option-a-v2",
        "token_hash": _hash(TOKEN),
        "scopes": list(SCOPES),
        "revoked_at": None,
        "expires_at": expires_at,
    }


class ExistingAccountExpiry(unittest.TestCase):
    """Same token, same 4 scopes, active: READY only with a real, bounded expiry."""

    def assert_refused(self, expires_at: Any, reason: str) -> None:
        code, out, db = run_bootstrap([active_row(expires_at)])
        self.assertEqual(code, 1, out)
        self.assertNotIn("SERVICE_ACCOUNT_READY", out)
        self.assertIn(reason, out)
        self.assertEqual(db.created, [], "a refusal must not create anything")

    def test_null_expiry_is_refused(self) -> None:
        # agno treats NULL as "never expires" and its column is nullable. This
        # was READY (exit 0) before the fix, and the token authenticated forever.
        self.assert_refused(None, "has no expiry")

    def test_expired_is_refused(self) -> None:
        self.assert_refused(int(time.time()) - 1, "has expired")

    def test_expiry_now_is_refused(self) -> None:
        self.assert_refused(int(time.time()), "has expired")

    def test_far_future_expiry_is_refused(self) -> None:
        self.assert_refused(int(time.time()) + 400 * DAY, "beyond the 30-day grant")

    def test_non_integer_expiry_is_refused(self) -> None:
        self.assert_refused("2099-01-01", "non-integer expiry")
        self.assert_refused(float(time.time() + DAY), "non-integer expiry")
        self.assert_refused(True, "non-integer expiry")

    def test_valid_expiry_is_ready(self) -> None:
        code, out, db = run_bootstrap([active_row(int(time.time()) + 30 * DAY)])
        self.assertEqual(code, 0, out)
        self.assertIn("WHOS_AGENTOS_SERVICE_ACCOUNT_READY name=whos-pee-option-a-v2 scopes=4", out)
        self.assertEqual(db.created, [])


class FreshAccount(unittest.TestCase):
    def test_create_issues_a_bounded_expiry(self) -> None:
        before = int(time.time())
        code, out, db = run_bootstrap([])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(db.created), 1)
        expires_at = db.created[0]["expires_at"]
        self.assertIsInstance(expires_at, int)
        self.assertGreaterEqual(expires_at, before + 30 * DAY)
        self.assertLessEqual(expires_at, int(time.time()) + 30 * DAY)
        # What it just created is exactly what a later deploy accepts.
        code, out, _ = run_bootstrap(db.rows)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
