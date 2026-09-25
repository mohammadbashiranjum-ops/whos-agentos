#!/usr/bin/env python3
"""
Bounded pre-deploy script: Idempotent whos-pee-option-a-v2 service account bootstrap.
- Creates whos-pee-option-a-v2 if absent, using WHOS_PEE_AGENTOS_TOKEN
- Verifies hash match if already exists
- Outputs marker: WHOS_AGENTOS_SERVICE_ACCOUNT_READY name=whos-pee-option-a-v2 scopes=4
- Does NOT print or expose any secret value or hash.
"""

import os
import sys
import hashlib
import json
from urllib.parse import urljoin

# Read environment
token = os.environ.get("WHOS_PEE_AGENTOS_TOKEN", "").strip()
agentos_url = os.environ.get("AGENTOS_URL", "").strip()
jwt_key = os.environ.get("JWT_VERIFICATION_KEY", "").strip()

if not token or not agentos_url:
    print("BLOCKER: Missing WHOS_PEE_AGENTOS_TOKEN or AGENTOS_URL", file=sys.stderr)
    sys.exit(1)

# Compute token hash (for comparison only, never printed)
token_hash = hashlib.sha256(token.encode()).hexdigest()

# Service account details
sa_name = "whos-pee-option-a-v2"
sa_scopes = ["config:read", "sessions:read", "agents:platform-engineer:read", "agents:platform-engineer:run"]
sa_expiry_days = 30
sa_created_by = "owner-approved-whos-activation"

try:
    import requests
    
    # Prepare headers with Bearer token
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    
    # Try to GET existing service account
    sa_url = urljoin(agentos_url, f"/api/service-accounts/{sa_name}")
    resp_get = requests.get(sa_url, headers=headers, timeout=10)
    
    if resp_get.status_code == 200:
        # Service account exists
        existing = resp_get.json()
        existing_hash = existing.get("token_hash", "")
        
        if existing_hash == token_hash:
            # Hash matches, idempotent: no change needed
            print(f"WHOS_AGENTOS_SERVICE_ACCOUNT_READY name={sa_name} scopes={len(sa_scopes)}")
            sys.exit(0)
        else:
            # Hash mismatch: STOP without mutation
            print(f"BLOCKER: {sa_name} exists with mismatched token hash. Aborting mutation.", file=sys.stderr)
            sys.exit(1)
    
    elif resp_get.status_code == 404:
        # Service account absent, create it
        create_payload = {
            "name": sa_name,
            "scopes": sa_scopes,
            "expiry_days": sa_expiry_days,
            "token_hash": token_hash,
            "created_by": sa_created_by,
        }
        
        resp_create = requests.post(
            urljoin(agentos_url, "/api/service-accounts"),
            json=create_payload,
            headers=headers,
            timeout=10,
        )
        
        if resp_create.status_code in (200, 201):
            print(f"WHOS_AGENTOS_SERVICE_ACCOUNT_READY name={sa_name} scopes={len(sa_scopes)}")
            sys.exit(0)
        else:
            print(f"BLOCKER: Failed to create {sa_name}: {resp_create.status_code} {resp_create.text}", file=sys.stderr)
            sys.exit(1)
    
    else:
        print(f"BLOCKER: Unexpected response: {resp_get.status_code} {resp_get.text}", file=sys.stderr)
        sys.exit(1)

except Exception as e:
    print(f"BLOCKER: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)

