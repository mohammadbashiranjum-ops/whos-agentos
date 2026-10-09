# Parallel Execution integration

AgentOS exposes an **on-demand** `parallel-execution` workflow. It triggers one configured Hatchet runnable with a gate-derived WHOS handoff and, concurrently, sends the separate `message` to one to three preconfigured LibreFang agents. It waits for the Hatchet result and each LibreFang response. The workflow is not scheduled and does not run at application startup.

## Safety and activation

`WHOS_PARALLEL_EXECUTION_ENABLED` defaults to `false`. Credentials alone do not activate dispatch. Setting it to `true` enables external calls and may incur model/provider charges; do so only after the service owner has explicitly approved the activation. AgentOS validates the handoff's schema and size, but that validation is **not authorization**. The existing WHOS Hatchet worker must continue to re-read the canonical gate evidence, verify the handoff digest, and take its durable exactly-once fence before work. This integration does not change or bypass those checks.

Handoff identity fields must be strings, the path list and each path are bounded, and the complete envelope is limited to 64 KiB. Hatchet's gRPC receive size is capped at 256 KiB. Raw Hatchet result content is never copied into the AgentOS workflow response; only whether a result was present and its type are returned.

The LibreFang agent IDs are selected from a deployment allowlist, not from request input; at most three agents can be called. **LibreFang's `/message` endpoint runs a full agent turn and may execute its configured tools or spend provider budget. This workflow forwards the caller's `message` unchanged and does not enforce read-only behavior.** Keep the feature disabled until the Owner verifies the deployed allowlisted agents' tools, ACL/ownership, and model access are limited to the intended actions. Neither the allowlist nor a read-only instruction in the prompt is a service-side permission boundary. Admission control allows one active combined execution per AgentOS process; concurrent requests return `busy` without calling providers. The current `railway.json` has one replica, but this lock is not distributed—do not scale horizontally until shared coordination is added. The workflow step has automatic retries disabled to avoid duplicate provider calls. LibreFang's message API does not provide an idempotency guarantee; the deterministic session ID isolates the request but does not make a repeated manual run free or exactly-once.

The workflow is served behind AgentOS's normal JWT authorization. Agno checks the requested workflow resource scope. The separately merged AgentOS PR #13 grants `workflows:read` and only the resource-specific `workflows:parallel-execution:run` scope to `whos-pee-option-a-v3`; it does not grant global `workflows:run`. Confirm authenticated list/run behavior after the deployment. Do not enable provider dispatch based only on workflow registration or authorization success.

## Required AgentOS environment variables

Set these on the **AgentOS service** in Railway. Keep token/key values in Railway's variable manager; never commit them or send them in chat.

| Variable | Required when enabled | Purpose |
|---|---:|---|
| `WHOS_PARALLEL_EXECUTION_ENABLED` | Yes | Explicit activation switch. Must be exactly `true` (case-insensitive); otherwise no provider clients or network calls are made. Default: disabled. |
| `HATCHET_TOKEN` | Yes | Tenant-scoped Hatchet API token, equivalent to the SDK token. |
| `HATCHET_URL` | Yes | Hatchet **gRPC control-plane** host and port, supplied as an absolute HTTPS origin such as `https://<hatchet-control-plane>:<grpc-port>`. It is not the worker's HTTP health URL. Plain HTTP is accepted only for loopback development. |
| `HATCHET_RUNNABLE_NAME` | Yes | Exact registered Hatchet workflow or standalone task name. |
| `HATCHET_RUNNABLE_KIND` | No | `workflow` or `standalone`; defaults to `workflow`. The existing WHOS worker currently registers standalone tasks, including `whos-execute-authorized-unit`. |
| `LIBREFANG_URL` | Yes | LibreFang API origin, without `/api` or a path (for example, `https://<librefang-runtime-domain>`). HTTPS is required outside loopback development. |
| `LIBREFANG_API_KEY` | Yes | LibreFang service API key; sent only as an HTTPS Bearer header. |
| `LIBREFANG_AGENT_IDS` | Yes | Comma-separated allowlist of 1–3 existing LibreFang agent IDs. These IDs cannot be supplied or overridden by a workflow caller. |

The wrapper parses `HATCHET_URL` into a scheme-less gRPC `host:port` target, passes that as `ClientConfig.host_port`, and explicitly sets `ClientTLSConfig.server_name` to the parsed hostname. It does not mutate process-wide SDK environment variables.

## Running it

Submit an AgentOS workflow run using a caller JWT that is authorized for this workflow:

```http
POST /workflows/parallel-execution/runs
Authorization: Bearer <AgentOS-JWT>
Idempotency-Key: <hatchet_payload.idempotency_key>
Content-Type: application/json
```

```json
{
  "message": "Summarize the authorized work unit and return a concise report.",
  "hatchet_payload": {
    "claim_id": "<gate-derived-claim>",
    "claim_evidence_id": "<canonical-evidence-id>",
    "task_id": "<task-id>",
    "command_id": "<command-id>",
    "work_unit": "<work-unit>",
    "lane": "<lane>",
    "generation": 1,
    "source_run": "<source-run>",
    "source_head": "<40-char-commit-sha>",
    "write_targets": ["<authorized-path>"],
    "traversal_id": "<gate-derived-traversal>",
    "idempotency_key": "<gate-derived-idempotency-key>",
    "owner_acknowledgement_evidence_id": "<owner-ack-id>",
    "owner_delivery_id": "<owner-delivery-id>",
    "handoff_digest": "<64-char-sha256>"
  }
}
```

Use the exact object produced by the WHOS backend handoff adapter; do not construct a handoff by hand. Hatchet receives only that object. LibreFang receives only the separate `message`, not the handoff fields. The Hatchet result body is omitted; each configured LibreFang response is clipped to 20,000 characters. Partial provider failure is surfaced as `partial_failure`, without automatic retry.

## What is not configured by this change

This code does not create Railway services, set Hatchet/LibreFang secrets, enable provider execution, start a live Hatchet run, or send a message to a live LibreFang agent. The least-privilege service-account scopes are handled separately by PR #13. Provider activation still requires verified endpoint/credential configuration and a confirmed effective tool-disabled policy for the allowlisted LibreFang agents. The integration can be exercised locally with the hermetic tests in `scripts/test_parallel_execution_integration.py`; those tests use fake clients and make no network calls.
