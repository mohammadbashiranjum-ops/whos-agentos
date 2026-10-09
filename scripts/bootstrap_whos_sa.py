#!/usr/bin/env python3
"""
Idempotent AgentOS service account bootstrap for WHOS PEE service accounts.

Uses Agno's native PostgresDb.create_service_account and ServiceAccount model
for consistent schema and behavior.

Behavior:
- If the selected account is absent (and not revoked): create from WHOS_PEE_AGENTOS_TOKEN
- If active account exists with matching token hash, scopes, and not expired: idempotent success
- If active account exists with a different token hash or expired: fail closed
- The exact owner-approved v3 account can transition once from its exact v2 scope set
  to the bounded read scopes and its own workflow's run scope, using a compare-and-swap
- All other scope mismatches fail closed
- If no active account but revoked account exists: fail closed, no mutation
- If any account exists with different state: never recreate, fail closed

After successful creation/validation, prints:
  WHOS_AGENTOS_SERVICE_ACCOUNT_READY name=<account> scopes=<count> scope_list=<comma-separated scopes>

Token must start with 'agno_pat_' prefix.

Renewal (the account is time-bounded to 30 days, and every path above fails
closed, so an expired or rotated account can never be reused or overwritten):
the account name is versioned. WHOS_PEE_AGENTOS_SA_NAME selects it, default
whos-pee-option-a-v2, and must match whos-pee-option-a-v<N>. Renewing is a new
version with a new token -- set WHOS_PEE_AGENTOS_SA_NAME=whos-pee-option-a-v3
and a freshly minted WHOS_PEE_AGENTOS_TOKEN, then deploy. The new account is
created exactly as above; the old one is left to expire (or be revoked) and is
never touched. No row is deleted and no pre-deploy setting changes.
The script itself never prints the plaintext token, its hash, or its prefix. A
token that already belongs to any account is refused before an insert is tried,
because agno's own logger echoes the insert parameters (hash and display prefix)
when the database rejects one.
Uses expires_at <= now_epoch for expiry check (Agno v3.0.4 convention). READY also
requires a real, bounded expiry: agno treats expires_at NULL as "never expires"
and its schema allows NULL, so an account whose expiry is missing, non-integer, or
further out than one grant (plus a day of clock slack) is refused, not reused.
"""

import os
import re
import sys
import time
from typing import Any
from uuid import uuid4

from agno.db.schemas.service_accounts import ServiceAccount
from agno.os.service_accounts import TOKEN_DISPLAY_PREFIX_LENGTH, hash_token

from db import get_postgres_db

VALIDITY_SECONDS = 30 * 24 * 60 * 60
EXPIRY_SLACK_SECONDS = 24 * 60 * 60
V3_ACCOUNT_NAME = "whos-pee-option-a-v3"
BASE_SCOPES = [
    "config:read",
    "sessions:read",
    "agents:platform-engineer:read",
    "agents:platform-engineer:run",
]
V3_SCOPES = [
    *BASE_SCOPES,
    "agents:read",
    "teams:read",
    "workflows:read",
    "workflows:parallel-execution:run",
]


def scopes_for_account(sa_name: str) -> list[str]:
    """Return the least-privilege profile for this immutable account version."""
    return list(V3_SCOPES if sa_name == V3_ACCOUNT_NAME else BASE_SCOPES)


def upgrade_v3_scopes_once(db: Any, active_sa: dict[str, Any], token_hash: str) -> str | None:
    """CAS-grant only the owner-approved v3 account from the exact legacy set.

    Agno deliberately treats service-account scopes as immutable through its public
    update API. This narrowly scoped migration is the explicit exception authorized
    for v3: match name, row id, token hash, active state and exact old scopes in one
    SQL UPDATE, then verify the committed scopes by reading the row back. It never
    changes the token, expiry, or any other account.
    """
    if active_sa.get("name") != V3_ACCOUNT_NAME:
        return "scope migration is restricted to the approved v3 account"
    account_id = active_sa.get("id")
    if not isinstance(account_id, str) or not account_id:
        return "account has no valid id for the guarded scope migration"
    if active_sa.get("token_hash") != token_hash:
        return "account token hash does not match the configured token"
    if active_sa.get("revoked_at") is not None:
        return "account is revoked"
    if active_sa.get("scopes") != BASE_SCOPES:
        return "account does not have the exact legacy scope set"

    table = db._get_table(table_type="service_accounts")
    if table is None:
        return "service-account table is unavailable"
    with db.Session() as session, session.begin():
        result = session.execute(
            table.update()
            .where(
                table.c.id == account_id,
                table.c.name == V3_ACCOUNT_NAME,
                table.c.token_hash == token_hash,
                table.c.revoked_at.is_(None),
                table.c.scopes == BASE_SCOPES,
            )
            .values(scopes=V3_SCOPES)
        )

    updated = db.get_service_account_by_name(V3_ACCOUNT_NAME, include_revoked=True)
    if (
        not updated
        or updated.get("id") != account_id
        or updated.get("name") != V3_ACCOUNT_NAME
        or updated.get("token_hash") != token_hash
        or updated.get("revoked_at") is not None
        or updated.get("scopes") != V3_SCOPES
    ):
        # A concurrent identical migration is harmless; accept only if the
        # authoritative read-back is already the exact intended state.
        if getattr(result, "rowcount", 0) != 0:
            return "scope migration read-back did not match the requested state"
        return "scope migration compare-and-swap matched no row"
    return None


def print_ready(sa_name: str, sa_scopes: list[str]) -> None:
    """Emit auditable permission names, never credential material."""
    print(f"WHOS_AGENTOS_SERVICE_ACCOUNT_READY name={sa_name} scopes={len(sa_scopes)} scope_list={','.join(sa_scopes)}")


def expiry_refusal(expires_at: object, now_epoch: int) -> str | None:
    """Why an existing account's expiry cannot be accepted, or None if it can.

    agno's ServiceAccount.is_expired() returns False for expires_at None, and the
    column is nullable, so a NULL expiry authenticates forever. This bootstrap only
    issues 30-day accounts, so READY demands exactly that: an integer epoch in the
    future, no further out than one grant plus a day of clock slack.
    """
    if expires_at is None:
        return "has no expiry (would never expire)"
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        return "has a non-integer expiry"
    if expires_at <= now_epoch:
        return "has expired"
    if expires_at > now_epoch + VALIDITY_SECONDS + EXPIRY_SLACK_SECONDS:
        return "has an expiry beyond the 30-day grant"
    return None


def main():
    # Read and validate token
    token_value = os.getenv("WHOS_PEE_AGENTOS_TOKEN")
    if not token_value:
        print("ERROR: WHOS_PEE_AGENTOS_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    if not token_value.startswith("agno_pat_"):
        print("ERROR: Token must start with 'agno_pat_'", file=sys.stderr)
        sys.exit(1)

    # Compute hash and prefix
    token_hash = hash_token(token_value)
    token_prefix = token_value[:TOKEN_DISPLAY_PREFIX_LENGTH]

    # Get database
    try:
        db = get_postgres_db()
    except Exception as e:
        print(f"ERROR: Failed to initialize database: {e}", file=sys.stderr)
        sys.exit(1)

    # Service account details
    sa_name = os.getenv("WHOS_PEE_AGENTOS_SA_NAME") or "whos-pee-option-a-v2"
    if not re.fullmatch(r"whos-pee-option-a-v[1-9][0-9]*", sa_name):
        print("ERROR: WHOS_PEE_AGENTOS_SA_NAME must match whos-pee-option-a-v<N>", file=sys.stderr)
        sys.exit(1)
    sa_scopes = scopes_for_account(sa_name)
    now_epoch = int(time.time())
    expires_at = now_epoch + VALIDITY_SECONDS
    created_by = "owner-approved-whos-activation"

    try:
        # A token is one credential for one account. Checked first, and on a read
        # that raises instead of returning None, so a database that cannot answer
        # stops the script here rather than letting the checks below read "absent".
        owner = db.get_service_account_by_token_hash(token_hash)
        if owner and owner.get("name") != sa_name:
            print(
                f"ERROR: Token already belongs to another service account; mint a new one for {sa_name}. Not mutating.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Then: check for an active (not revoked) account by name
        active_sa = db.get_service_account_by_name(sa_name, include_revoked=False)

        if active_sa:
            # Active account exists: validate idempotency
            existing_hash = active_sa.get("token_hash")
            if existing_hash != token_hash:
                print(
                    f"ERROR: Service account {sa_name} exists with different token hash. Not mutating.",
                    file=sys.stderr,
                )
                sys.exit(1)

            refusal = expiry_refusal(active_sa.get("expires_at"), now_epoch)
            if refusal:
                print(f"ERROR: Service account {sa_name} {refusal}. Not mutating.", file=sys.stderr)
                sys.exit(1)

            existing_scopes = active_sa.get("scopes")
            if sa_name == V3_ACCOUNT_NAME and existing_scopes == BASE_SCOPES:
                grant_refusal = upgrade_v3_scopes_once(db, active_sa, token_hash)
                if grant_refusal:
                    print(
                        f"ERROR: Service account {sa_name} scope upgrade refused: {grant_refusal}. Not mutating.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                active_sa = db.get_service_account_by_name(sa_name, include_revoked=False)
                existing_scopes = active_sa.get("scopes") if active_sa else None

            # Fail-closed: require an exact, duplicate-free profile for this version.
            if (
                not isinstance(existing_scopes, list)
                or len(existing_scopes) != len(sa_scopes)
                or len(set(existing_scopes)) != len(existing_scopes)
                or set(existing_scopes) != set(sa_scopes)
            ):
                print(
                    f"ERROR: Service account {sa_name} exists with different scopes. Not mutating.",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Idempotent success
            print_ready(sa_name, sa_scopes)
            sys.exit(0)

        # No active account: check if revoked account exists
        latest_any = db.get_service_account_by_name(sa_name, include_revoked=True)

        if latest_any:
            # Account exists but is revoked: fail closed, do not recreate
            revoked_at = latest_any.get("revoked_at")
            if revoked_at is not None:
                print(
                    f"ERROR: Service account {sa_name} is revoked. Not mutating.",
                    file=sys.stderr,
                )
                sys.exit(1)

        # No account exists at all: create new active account
        sa_id = str(uuid4())
        new_sa = ServiceAccount(
            id=sa_id,
            name=sa_name,
            token_hash=token_hash,
            token_prefix=token_prefix,
            scopes=sa_scopes,
            created_by=created_by,
            expires_at=expires_at,
        )

        db.create_service_account(new_sa.to_dict())

        print_ready(sa_name, sa_scopes)
        sys.exit(0)

    except Exception as e:
        # The exception's class only: a database error's text carries the SQL
        # parameters, which here include the token hash and display prefix.
        print(f"ERROR: Service account bootstrap failed: {type(e).__name__}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
