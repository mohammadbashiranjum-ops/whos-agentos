# WHOS PEE AgentOS service-account permissions

This describes the permission profile for `whos-pee-option-a-v3`. The pre-deploy bootstrap reads `WHOS_PEE_AGENTOS_SA_NAME` and `WHOS_PEE_AGENTOS_TOKEN`. It contains scope names only; never put the token or token hash in this document, a commit, or chat.

## Exact scope set

The deployed profile is the existing four scopes plus these four grants:

| Scope | Purpose |
|---|---|
| `config:read` | Existing configuration access. |
| `sessions:read` | Existing session access. |
| `agents:platform-engineer:read` | Existing read access to the Platform Engineer agent. |
| `agents:platform-engineer:run` | Existing run access to the Platform Engineer agent. |
| `agents:read` | List/read all three AgentOS agents (`GET /agents`). |
| `teams:read` | List/read the AgentOS team (`GET /teams`). |
| `workflows:read` | List/read registered workflows (`GET /workflows`). |
| `workflows:parallel-execution:run` | Run only the `parallel-execution` workflow. |

The run permission is resource-specific. It does **not** grant `workflows:run` for every workflow, nor create/edit/delete any resource.

## Permissions intentionally not granted

`agents:run`, `teams:run`, global `workflows:run`, `schedules:read`, `schedules:run` (Agno uses `schedules:write` for schedule mutation/triggering), service-account management, and any admin/wildcard scope are unnecessary for the requested list checks and on-demand PEE workflow. Do not add them unless a separately approved need is established.

## Existing-account transition

Agno's public service-account update API intentionally treats scopes as immutable. The pre-deploy bootstrap therefore supports only this explicit, bounded migration: when `WHOS_PEE_AGENTOS_SA_NAME` is exactly `whos-pee-option-a-v3`, the configured token hash must match the active row, its expiry must be valid, and the row must contain exactly the original four-scope profile. It then performs one atomic compare-and-swap update of the `scopes` column and reads the row back. It preserves the token, token hash, expiry, account ID, and revocation state. An already-upgraded row is idempotent. Any other name, credential, scope set, expiry, or CAS/read-back mismatch fails closed; no other service account is changed. Other account versions retain their original four-scope profile.

The bootstrap log prints only the account name and exact scope names (never credential material), providing deployment evidence for the granted set.

## Verification after deployment

Using the Owner-provided v3 bearer token through a secure client (do not log it):

1. `GET /workflows` should return HTTP 200 and, after the PEE integration deploy, list the existing two workflows plus `parallel-execution`.
2. `GET /agents` should return HTTP 200 with 3 agents.
3. `GET /teams` should return HTTP 200 with 1 team.
4. `POST /workflows/parallel-execution/runs` is authorized only for the PEE workflow. The workflow remains fail-closed until its explicit feature flag, Hatchet endpoint/token, LibreFang endpoint/key/agent allowlist, and the agents' actual tool-disabled permissions are verified. Listing and authorization success alone are not evidence of a live provider run.
