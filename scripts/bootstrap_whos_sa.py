#!/usr/bin/env python3
"""
Idempotent AgentOS service account bootstrap for whos-pee-option-a-v2.

Uses Agno's native PostgresDb.create_service_account and ServiceAccount model
for consistent schema and behavior.

Behavior:
- If whos-pee-option-a-v2 is absent (and not revoked): create from WHOS_PEE_AGENTOS_TOKEN
- If active account exists with matching token hash, scopes, and not expired: idempotent success
- If active account exists with different token hash, scopes, or expired: fail closed, no mutation
- If no active account but revoked account exists: fail closed, no mutation
- If any account exists with different state: never recreate, fail closed

After successful creation/validation, prints:
  WHOS_AGENTOS_SERVICE_ACCOUNT_READY name=whos-pee-option-a-v2 scopes=4

Token must start with 'agno_pat_' prefix.
Never prints plaintext token, hash, or prefix.
Uses expires_at <= now_epoch for expiry check (Agno v3.0.4 convention).
"""

import os
import sys
import time
from uuid import uuid4

from agno.db.schemas.service_accounts import ServiceAccount
from agno.os.service_accounts import TOKEN_DISPLAY_PREFIX_LENGTH, hash_token

from db import get_postgres_db


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
    sa_name = "whos-pee-option-a-v2"
    sa_scopes = [
        "config:read",
        "sessions:read",
        "agents:platform-engineer:read",
        "agents:platform-engineer:run",
    ]
    now_epoch = int(time.time())
    expires_at = now_epoch + (30 * 24 * 60 * 60)
    created_by = "owner-approved-whos-activation"

    try:
        # First query: check for active (not revoked) account
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

            existing_scopes = active_sa.get("scopes") or []
            if set(existing_scopes) != set(sa_scopes):
                print(
                    f"ERROR: Service account {sa_name} exists with different scopes. Not mutating.",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Check expiry: expires_at <= now_epoch is expired
            existing_expiry = active_sa.get("expires_at")
            if existing_expiry is not None and existing_expiry <= now_epoch:
                print(
                    f"ERROR: Service account {sa_name} has expired. Not mutating.",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Idempotent success
            print(f"WHOS_AGENTOS_SERVICE_ACCOUNT_READY name={sa_name} scopes={len(sa_scopes)}")
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

        # No account at all (or only revoked): create new active account
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

        print(f"WHOS_AGENTOS_SERVICE_ACCOUNT_READY name={sa_name} scopes={len(sa_scopes)}")
        sys.exit(0)

    except Exception as e:
        print(f"ERROR: Service account bootstrap failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

