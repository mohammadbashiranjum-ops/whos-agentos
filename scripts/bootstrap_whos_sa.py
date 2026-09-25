#!/usr/bin/env python3
"""
Idempotent AgentOS service account bootstrap for whos-pee-option-a-v2.

Behavior:
- If whos-pee-option-a-v2 is absent: create it from WHOS_PEE_AGENTOS_TOKEN
- If it exists and hash matches: no change
- If it exists with different hash: STOP without mutating

After successful creation/validation, prints:
  WHOS_AGENTOS_SERVICE_ACCOUNT_READY name=whos-pee-option-a-v2 scopes=4
"""

import os
import sys
import hashlib
import json
from datetime import datetime, timedelta

# Read environment
token_value = os.getenv("WHOS_PEE_AGENTOS_TOKEN")
if not token_value:
    print("ERROR: WHOS_PEE_AGENTOS_TOKEN not set", file=sys.stderr)
    sys.exit(1)

# Compute hash (never print the actual token)
token_hash = hashlib.sha256(token_value.encode()).hexdigest()

# Connect to database
db_host = os.getenv("DB_HOST")
db_port = os.getenv("DB_PORT", "5432")
db_user = os.getenv("DB_USER")
db_pass = os.getenv("DB_PASS")
db_name = os.getenv("DB_DATABASE")

if not all([db_host, db_user, db_pass, db_name]):
    print("ERROR: Database credentials incomplete", file=sys.stderr)
    sys.exit(1)

try:
    import psycopg2
except ImportError:
    print("ERROR: psycopg2 not available; cannot bootstrap service account", file=sys.stderr)
    sys.exit(1)

try:
    conn = psycopg2.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_pass,
        database=db_name,
    )
    cursor = conn.cursor()
except Exception as e:
    print(f"ERROR: Failed to connect to database: {e}", file=sys.stderr)
    sys.exit(1)

sa_name = "whos-pee-option-a-v2"
sa_scopes = ["config:read", "sessions:read", "agents:platform-engineer:read", "agents:platform-engineer:run"]
expiry_date = (datetime.utcnow() + timedelta(days=30)).isoformat()

try:
    # Check if service account already exists
    cursor.execute(
        "SELECT name, token_hash FROM service_accounts WHERE name = %s",
        (sa_name,),
    )
    existing = cursor.fetchone()

    if existing:
        existing_name, existing_hash = existing
        if existing_hash == token_hash:
            # Hash matches: idempotent success
            print(f"WHOS_AGENTOS_SERVICE_ACCOUNT_READY name={sa_name} scopes={len(sa_scopes)}")
            cursor.close()
            conn.close()
            sys.exit(0)
        else:
            # Hash mismatch: STOP
            print(
                f"ERROR: Service account {sa_name} exists with different token hash. Not mutating.",
                file=sys.stderr,
            )
            cursor.close()
            conn.close()
            sys.exit(1)
    
    # Service account does not exist: create it
    scopes_json = json.dumps(sa_scopes)
    cursor.execute(
        """
        INSERT INTO service_accounts
        (name, token_hash, scopes, created_by, expiry_date, created_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        """,
        (sa_name, token_hash, scopes_json, "owner-approved-whos-activation", expiry_date),
    )
    conn.commit()
    
    print(f"WHOS_AGENTOS_SERVICE_ACCOUNT_READY name={sa_name} scopes={len(sa_scopes)}")
    cursor.close()
    conn.close()
    sys.exit(0)

except Exception as e:
    print(f"ERROR: Service account bootstrap failed: {e}", file=sys.stderr)
    try:
        conn.rollback()
        cursor.close()
        conn.close()
    except:
        pass
    sys.exit(1)

