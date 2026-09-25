#!/usr/bin/env python3
"""
Idempotent AgentOS service account bootstrap for whos-pee-option-a-v2.

Uses Agno's native PostgresDb.create_service_account and ServiceAccount model
for consistent schema and behavior.

Behavior:
- If whos-pee-option-a-v2 is absent: create it from WHOS_PEE_AGENTOS_TOKEN
- If it exists (not revoked) with matching token hash and scopes, and not expired:
  idempotent success, no change
- If it exists (not revoked) with different token hash, different scopes, or expired:
  fail closed, do not mutate
- If revoked: fail closed, do not mutate

After successful creation/validation, prints:
  WHOS_AGENTOS_SERVICE_ACCOUNT_READY name=whos-pee-option-a-v2 scopes=4

Token must start with 'agno_pat_' prefix.
Never prints plaintext token, hash, or prefix.
"""

import os
import sys
from datetime import datetime, timedelta
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
    expires_at = int((datetime.utcnow() + timedelta(days=30)).timestamp())
    created_by = "owner-approved-whos-activation"

    try:
        # Check if service account already exists (not revoked)
        existing_sa = db.get_service_account_by_name(sa_name, include_revoked=False)

        if existing_sa:
            # Validate: token hash, scopes, and expiry (use dict.get() for safe access)
            existing_hash = existing_sa.get("token_hash")
            if existing_hash != token_hash:
                print(
                    f"ERROR: Service account {sa_name} exists with different token hash. Not mutating.",
                    file=sys.stderr,
                )
                sys.exit(1)

            existing_scopes = existing_sa.get("scopes") or []
            if set(existing_scopes) != set(sa_scopes):
                print(
                    f"ERROR: Service account {sa_name} exists with different scopes. Not mutating.",
                    file=sys.stderr,
                )
                sys.exit(1)

            existing_expiry = existing_sa.get("expires_at")
            if existing_expiry is not None and existing_expiry < int(datetime.utcnow().timestamp()):
                print(
                    f"ERROR: Service account {sa_name} has expired. Not mutating.",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Idempotent success
            print(f"WHOS_AGENTOS_SERVICE_ACCOUNT_READY name={sa_name} scopes={len(sa_scopes)}")
            sys.exit(0)

        # Service account does not exist: create it
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
