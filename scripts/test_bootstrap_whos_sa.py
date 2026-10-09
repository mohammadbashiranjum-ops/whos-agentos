"""Regression tests for scripts/bootstrap_whos_sa.py.

Hermetic: the real script is loaded with stand-ins for Agno's service-account
module and the app database, so these run anywhere with the standard library.
They drive main() end to end and verify exact grant/refusal decisions, including
the one explicitly owner-approved, compare-and-swap migration for v3.

Run: python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

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
V3_NAME = "whos-pee-option-a-v3"
BASE_SCOPES = [
    "config:read",
    "sessions:read",
    "agents:platform-engineer:read",
    "agents:platform-engineer:run",
]
ADDED_SCOPES = [
    "agents:read",
    "teams:read",
    "workflows:read",
    "workflows:parallel-execution:run",
]
V3_SCOPES = [*BASE_SCOPES, *ADDED_SCOPES]
DAY = 24 * 60 * 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class FakeColumn:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, value: object) -> Any:
        return ("eq", self.name, value)

    def is_(self, value: object) -> tuple[str, str, object]:
        return ("is", self.name, value)


class FakeUpdate:
    def __init__(self):
        self.predicates: list[tuple[str, str, object]] = []
        self.values_to_set: dict[str, object] = {}

    def where(self, *predicates: tuple[str, str, object]) -> FakeUpdate:
        self.predicates.extend(predicates)
        return self

    def values(self, **values: object) -> FakeUpdate:
        self.values_to_set = values
        return self


class FakeTable:
    def __init__(self):
        self.c = types.SimpleNamespace(
            id=FakeColumn("id"),
            name=FakeColumn("name"),
            token_hash=FakeColumn("token_hash"),
            revoked_at=FakeColumn("revoked_at"),
            scopes=FakeColumn("scopes"),
        )

    def update(self) -> FakeUpdate:
        return FakeUpdate()


class FakeSession:
    def __init__(self, db: FakeDb):
        self.db = db

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def begin(self) -> FakeSession:
        return self

    def execute(self, statement: FakeUpdate) -> types.SimpleNamespace:
        if self.db.fail_scope_update:
            return types.SimpleNamespace(rowcount=0)
        matched = [
            row
            for row in self.db.rows
            if all(
                row.get(column) == value if operator == "eq" else row.get(column) is value
                for operator, column, value in statement.predicates
            )
        ]
        for row in matched:
            row.update(statement.values_to_set)
        self.db.scope_updates += len(matched)
        return types.SimpleNamespace(rowcount=len(matched))


class FakeDb:
    """Small stand-in for the PostgresDb methods used by the bootstrap."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.created: list[dict[str, Any]] = []
        self.scope_updates = 0
        self.fail_scope_update = False
        self.active_lookup_miss_once = False
        self.active_name_lookups = 0
        self.table = FakeTable()

    def get_service_account_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        return next((row for row in self.rows if row["token_hash"] == token_hash), None)

    def get_service_account_by_name(self, name: str, include_revoked: bool = False) -> dict[str, Any] | None:
        if not include_revoked:
            self.active_name_lookups += 1
            if self.active_lookup_miss_once and self.active_name_lookups == 1:
                return None
        for row in self.rows:
            if row["name"] == name and (include_revoked or row.get("revoked_at") is None):
                return row
        return None

    def create_service_account(self, data: dict[str, Any]) -> None:
        self.created.append(data)
        self.rows.append(data)

    def _get_table(self, table_type: str) -> FakeTable | None:
        if table_type != "service_accounts":
            return None
        return self.table

    def Session(self) -> FakeSession:
        return FakeSession(self)


def load_script(db: FakeDb) -> types.ModuleType:
    """Import the real script with Agno's service-account API and DB stubbed."""

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


def run_bootstrap(
    rows: list[dict[str, Any]],
    token: str = TOKEN,
    account_name: str | None = None,
    *,
    fail_update: bool = False,
    active_lookup_miss_once: bool = False,
) -> tuple[int, str, FakeDb]:
    """Run main() once; return (exit code, combined output, the fake DB)."""
    db = FakeDb(rows)
    db.fail_scope_update = fail_update
    db.active_lookup_miss_once = active_lookup_miss_once
    module = load_script(db)
    out = io.StringIO()
    env = {"WHOS_PEE_AGENTOS_TOKEN": token}
    if account_name is not None:
        env["WHOS_PEE_AGENTOS_SA_NAME"] = account_name
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


def active_row(
    expires_at: Any,
    name: str = "whos-pee-option-a-v2",
    scopes: list[str] | None = None,
    token: str = TOKEN,
    revoked_at: Any = None,
) -> dict[str, Any]:
    return {
        "id": f"service-account-{name}",
        "name": name,
        "token_hash": _hash(token),
        "scopes": list(BASE_SCOPES if scopes is None else scopes),
        "revoked_at": revoked_at,
        "expires_at": expires_at,
    }


class ExistingAccountExpiry(unittest.TestCase):
    """The unchanged v2 profile is READY only with a real, bounded expiry."""

    def assert_refused(self, expires_at: Any, reason: str) -> None:
        code, out, db = run_bootstrap([active_row(expires_at)])
        self.assertEqual(code, 1, out)
        self.assertNotIn("SERVICE_ACCOUNT_READY", out)
        self.assertIn(reason, out)
        self.assertEqual(db.created, [])
        self.assertEqual(db.scope_updates, 0)

    def test_null_expiry_is_refused(self) -> None:
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

    def test_valid_expiry_is_ready_without_scope_change(self) -> None:
        code, out, db = run_bootstrap([active_row(int(time.time()) + 30 * DAY)])
        self.assertEqual(code, 0, out)
        self.assertIn("name=whos-pee-option-a-v2 scopes=4", out)
        self.assertIn("scope_list=" + ",".join(BASE_SCOPES), out)
        self.assertEqual(db.created, [])
        self.assertEqual(db.scope_updates, 0)


class V3ScopeGrant(unittest.TestCase):
    """Only the exact active v3 account can receive its approved scope delta."""

    def test_exact_legacy_state_upgrades_once_and_preserves_credential_and_expiry(self) -> None:
        row = active_row(int(time.time()) + 20 * DAY, name=V3_NAME)
        unrelated = active_row(
            int(time.time()) + 25 * DAY,
            name="unrelated-service-account",
            token="agno_pat_" + "z" * 64,
        )
        unrelated_before = dict(unrelated)
        original_id = row["id"]
        original_hash = row["token_hash"]
        original_expiry = row["expires_at"]
        code, out, db = run_bootstrap([row, unrelated], account_name=V3_NAME)
        self.assertEqual(code, 0, out)
        self.assertEqual(row["scopes"], V3_SCOPES)
        self.assertEqual(row["id"], original_id)
        self.assertEqual(row["token_hash"], original_hash)
        self.assertEqual(row["expires_at"], original_expiry)
        self.assertIsNone(row["revoked_at"])
        self.assertEqual(unrelated, unrelated_before)
        self.assertEqual(db.created, [])
        self.assertEqual(db.scope_updates, 1)
        self.assertIn(f"name={V3_NAME} scopes=8", out)
        self.assertIn("scope_list=" + ",".join(V3_SCOPES), out)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(original_hash, out)

    def test_already_granted_v3_is_idempotent(self) -> None:
        row = active_row(int(time.time()) + 20 * DAY, name=V3_NAME, scopes=V3_SCOPES)
        code, out, db = run_bootstrap([row], account_name=V3_NAME)
        self.assertEqual(code, 0, out)
        self.assertEqual(row["scopes"], V3_SCOPES)
        self.assertEqual(db.scope_updates, 0)
        self.assertEqual(db.created, [])

    def test_new_v3_is_created_with_only_its_eight_scopes(self) -> None:
        code, out, db = run_bootstrap([], account_name=V3_NAME)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(db.created), 1)
        self.assertEqual(db.created[0]["name"], V3_NAME)
        self.assertEqual(db.created[0]["scopes"], V3_SCOPES)
        self.assertIn("scope_list=" + ",".join(V3_SCOPES), out)

    def test_wrong_token_hash_refuses_without_grant(self) -> None:
        row = active_row(int(time.time()) + DAY, name=V3_NAME)
        code, out, db = run_bootstrap([row], token="agno_pat_" + "b" * 64, account_name=V3_NAME)
        self.assertEqual(code, 1, out)
        self.assertIn("different token hash", out)
        self.assertEqual(row["scopes"], BASE_SCOPES)
        self.assertEqual(db.scope_updates, 0)

    def test_unexpected_or_duplicate_scopes_refuse_without_grant(self) -> None:
        for scopes in (BASE_SCOPES + ["agents:write"], BASE_SCOPES + ["config:read"]):
            with self.subTest(scopes=scopes):
                row = active_row(int(time.time()) + DAY, name=V3_NAME, scopes=scopes)
                code, out, db = run_bootstrap([row], account_name=V3_NAME)
                self.assertEqual(code, 1, out)
                self.assertIn("different scopes", out)
                self.assertEqual(row["scopes"], scopes)
                self.assertEqual(db.scope_updates, 0)

    def test_invalid_expiry_refuses_before_grant(self) -> None:
        row = active_row(None, name=V3_NAME)
        code, out, db = run_bootstrap([row], account_name=V3_NAME)
        self.assertEqual(code, 1, out)
        self.assertIn("has no expiry", out)
        self.assertEqual(row["scopes"], BASE_SCOPES)
        self.assertEqual(db.scope_updates, 0)

    def test_compare_and_swap_miss_refuses_without_mutation(self) -> None:
        row = active_row(int(time.time()) + DAY, name=V3_NAME)
        code, out, db = run_bootstrap([row], account_name=V3_NAME, fail_update=True)
        self.assertEqual(code, 1, out)
        self.assertIn("compare-and-swap matched no row", out)
        self.assertEqual(row["scopes"], BASE_SCOPES)
        self.assertEqual(db.scope_updates, 0)

    def test_v2_account_never_gets_v3_scopes(self) -> None:
        row = active_row(int(time.time()) + DAY, scopes=V3_SCOPES)
        code, out, db = run_bootstrap([row])
        self.assertEqual(code, 1, out)
        self.assertIn("different scopes", out)
        self.assertEqual(row["scopes"], V3_SCOPES)
        self.assertEqual(db.scope_updates, 0)

    def test_revoked_v3_is_not_recreated_or_upgraded(self) -> None:
        row = active_row(int(time.time()) + DAY, name=V3_NAME, revoked_at=int(time.time()))
        code, out, db = run_bootstrap([row], account_name=V3_NAME)
        self.assertEqual(code, 1, out)
        self.assertIn("is revoked", out)
        self.assertEqual(row["scopes"], BASE_SCOPES)
        self.assertEqual(db.created, [])
        self.assertEqual(db.scope_updates, 0)

    def test_active_row_found_by_inclusive_second_lookup_is_not_recreated(self) -> None:
        other_token = "agno_pat_" + "c" * 64
        row = active_row(int(time.time()) + DAY, name=V3_NAME, token=other_token)
        original = dict(row)
        code, out, db = run_bootstrap([row], account_name=V3_NAME, active_lookup_miss_once=True)
        self.assertEqual(code, 1, out)
        self.assertIn("unexpected existing state", out)
        self.assertEqual(db.created, [])
        self.assertEqual(db.scope_updates, 0)
        self.assertEqual(row, original)


class FreshAccount(unittest.TestCase):
    def test_v2_creation_keeps_the_original_four_scope_profile(self) -> None:
        before = int(time.time())
        code, out, db = run_bootstrap([])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(db.created), 1)
        expires_at = db.created[0]["expires_at"]
        self.assertIsInstance(expires_at, int)
        self.assertGreaterEqual(expires_at, before + 30 * DAY)
        self.assertLessEqual(expires_at, int(time.time()) + 30 * DAY)
        self.assertEqual(db.created[0]["scopes"], BASE_SCOPES)
        # What it just created is exactly what a later deploy accepts.
        code, out, _ = run_bootstrap(db.rows)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
