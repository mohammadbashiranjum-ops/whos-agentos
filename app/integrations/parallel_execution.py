"""Gated parallel dispatch to Hatchet and allowlisted LibreFang agents.

AgentOS checks the handoff's shape only. The WHOS Hatchet worker remains the
execution authority: it re-reads the canonical gate evidence, verifies the
handoff digest, and takes its durable exactly-once fence before doing work.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urlparse
from uuid import NAMESPACE_URL, uuid5

import httpx

ENABLE_ENV = "WHOS_PARALLEL_EXECUTION_ENABLED"
MAX_LIBREFANG_AGENTS = 3
MAX_HANDOFF_BYTES = 64 * 1024
MAX_LIBREFANG_RESPONSE_BYTES = 256 * 1024
MAX_LIBREFANG_RESPONSE_CHARS = 20_000
HATCHET_RESULT_TIMEOUT_SECONDS = 600.0
LIBREFANG_TIMEOUT_SECONDS = 120.0
LIBREFANG_TOTAL_TIMEOUT_SECONDS = 150.0
LIBREFANG_STREAM_CHUNK_BYTES = 64 * 1024
MAX_CONCURRENT_PARALLEL_EXECUTIONS_PER_PROCESS = 1
_EXECUTION_SLOT = threading.BoundedSemaphore(MAX_CONCURRENT_PARALLEL_EXECUTIONS_PER_PROCESS)

HANDOFF_FIELDS = (
    "claim_id",
    "claim_evidence_id",
    "task_id",
    "command_id",
    "work_unit",
    "lane",
    "generation",
    "source_run",
    "source_head",
    "write_targets",
    "traversal_id",
    "idempotency_key",
    "owner_acknowledgement_evidence_id",
    "owner_delivery_id",
    "handoff_digest",
)

_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+-]{0,255}$")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class IntegrationConfigurationError(ValueError):
    """A safe-to-display configuration error; secret values are never included."""

    def __init__(self, reason: str, *, missing: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.missing = missing


class ProviderResponseTooLargeError(ValueError):
    """A streamed provider response exceeded the bounded JSON body size."""


@dataclass(frozen=True)
class ParallelExecutionConfig:
    """Runtime settings for one on-demand, explicitly enabled fan-out."""

    enabled: bool
    hatchet_token: str = field(repr=False)
    hatchet_url: str = field(repr=False)
    hatchet_runnable_name: str
    hatchet_runnable_kind: str
    librefang_url: str = field(repr=False)
    librefang_api_key: str = field(repr=False)
    librefang_agent_ids: tuple[str, ...]

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ParallelExecutionConfig:
        """Load aliases used by this app without exposing secrets in logs or reprs.

        Anything other than the literal string ``true`` leaves dispatch disabled.
        Provider credentials are not even required until an operator enables it.
        """
        source = environ if environ is not None else os.environ
        enabled = source.get(ENABLE_ENV, "").strip().lower() == "true"
        if not enabled:
            return cls(
                enabled=False,
                hatchet_token="",
                hatchet_url="",
                hatchet_runnable_name="",
                hatchet_runnable_kind="workflow",
                librefang_url="",
                librefang_api_key="",
                librefang_agent_ids=(),
            )

        required = (
            "HATCHET_TOKEN",
            "HATCHET_URL",
            "HATCHET_RUNNABLE_NAME",
            "LIBREFANG_URL",
            "LIBREFANG_API_KEY",
            "LIBREFANG_AGENT_IDS",
        )
        missing = tuple(name for name in required if not source.get(name, "").strip())
        if missing:
            joined = ", ".join(missing)
            raise IntegrationConfigurationError(
                f"Parallel execution is enabled but required environment variables are missing: {joined}.",
                missing=missing,
            )

        hatchet_runnable_name = source["HATCHET_RUNNABLE_NAME"].strip()
        if not _NAME_PATTERN.fullmatch(hatchet_runnable_name):
            raise IntegrationConfigurationError("HATCHET_RUNNABLE_NAME must be a simple registered name.")

        runnable_kind = source.get("HATCHET_RUNNABLE_KIND", "workflow").strip().lower()
        if runnable_kind not in {"workflow", "standalone"}:
            raise IntegrationConfigurationError("HATCHET_RUNNABLE_KIND must be 'workflow' or 'standalone'.")

        raw_agent_ids = source["LIBREFANG_AGENT_IDS"].split(",")
        agent_ids = tuple(agent_id.strip() for agent_id in raw_agent_ids if agent_id.strip())
        if not agent_ids or len(agent_ids) > MAX_LIBREFANG_AGENTS:
            raise IntegrationConfigurationError(
                f"LIBREFANG_AGENT_IDS must list between 1 and {MAX_LIBREFANG_AGENTS} agents."
            )
        if len(set(agent_ids)) != len(agent_ids) or any(
            not _NAME_PATTERN.fullmatch(agent_id) for agent_id in agent_ids
        ):
            raise IntegrationConfigurationError("LIBREFANG_AGENT_IDS must contain unique simple agent IDs.")

        return cls(
            enabled=True,
            hatchet_token=source["HATCHET_TOKEN"].strip(),
            hatchet_url=_validate_base_url("HATCHET_URL", source["HATCHET_URL"]),
            hatchet_runnable_name=hatchet_runnable_name,
            hatchet_runnable_kind=runnable_kind,
            librefang_url=_validate_base_url("LIBREFANG_URL", source["LIBREFANG_URL"]),
            librefang_api_key=source["LIBREFANG_API_KEY"].strip(),
            librefang_agent_ids=agent_ids,
        )


def _validate_base_url(name: str, value: str) -> str:
    """Require a credential-free HTTPS origin; HTTP is loopback-only for local tests."""
    try:
        parsed = urlparse(value.strip())
        # Accessing .port validates malformed ports as well as bracketed IPv6 syntax.
        _ = parsed.port
    except ValueError as exc:
        raise IntegrationConfigurationError(f"{name} is not a valid absolute service URL.") from exc

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise IntegrationConfigurationError(
            f"{name} must be an absolute HTTP(S) service origin without path, query, fragment, or URL credentials."
        )
    if parsed.scheme == "http" and parsed.hostname.lower() not in _LOCAL_HOSTS:
        raise IntegrationConfigurationError(f"{name} must use HTTPS outside loopback development hosts.")
    return f"{parsed.scheme}://{parsed.netloc}"


def validate_whos_handoff(value: Any) -> dict[str, Any]:
    """Validate the signed/gate-derived handoff's envelope, not its authority.

    The worker performs the cryptographic/durable authorization checks again.
    AgentOS only refuses malformed, oversized, or caller-extended envelopes.
    """
    if not isinstance(value, Mapping):
        raise ValueError("hatchet_payload must be an object.")

    payload = dict(value)
    missing = [field for field in HANDOFF_FIELDS if field not in payload or payload[field] in (None, "", [])]
    if missing:
        raise ValueError(f"hatchet_payload is missing required handoff fields: {', '.join(missing)}.")
    unexpected = sorted(set(payload) - set(HANDOFF_FIELDS))
    if unexpected:
        raise ValueError("hatchet_payload contains fields outside the WHOS handoff contract.")
    if (
        isinstance(payload["generation"], bool)
        or not isinstance(payload["generation"], int)
        or payload["generation"] < 1
    ):
        raise ValueError("handoff generation must be a positive integer.")
    targets = payload["write_targets"]
    if (
        not isinstance(targets, list)
        or not targets
        or any(not isinstance(item, str) or not item.strip() for item in targets)
    ):
        raise ValueError("handoff write_targets must be a non-empty list of paths.")
    if not _IDENTITY_PATTERN.fullmatch(str(payload["idempotency_key"])):
        raise ValueError("handoff idempotency_key is not a safe identity.")
    if not _DIGEST_PATTERN.fullmatch(str(payload["handoff_digest"])):
        raise ValueError("handoff_digest must be a SHA-256 hex digest.")

    try:
        size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("hatchet_payload must be JSON serializable.") from exc
    if size > MAX_HANDOFF_BYTES:
        raise ValueError(f"hatchet_payload exceeds the {MAX_HANDOFF_BYTES}-byte limit.")
    return payload


@lru_cache(maxsize=4)
def _cached_hatchet_client(token: str, host_port: str, tls_strategy: str, server_name: str) -> Any:
    """Create a reusable SDK client lazily; no service connection occurs at import."""
    from hatchet_sdk import Hatchet
    from hatchet_sdk.config import ClientConfig, ClientTLSConfig

    config = ClientConfig(
        token=token,
        host_port=host_port,
        tls_config=ClientTLSConfig(strategy=tls_strategy, server_name=server_name),
    )
    return Hatchet(config=config)


def _hatchet_client(config: ParallelExecutionConfig) -> Any:
    parsed = urlparse(config.hatchet_url)
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target_host = f"[{hostname}]" if ":" in hostname else hostname
    host_port = f"{target_host}:{port}"
    tls_strategy = "tls" if parsed.scheme == "https" else "none"
    return _cached_hatchet_client(config.hatchet_token, host_port, tls_strategy, hostname)


def _remote_standalone_declaration(_input: Any, _ctx: Any) -> dict[str, Any]:
    """Local declaration only; AgentOS never runs this stub as a worker."""
    raise RuntimeError("This AgentOS client declaration is not a Hatchet worker.")


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str, allow_nan=False))
    except TypeError, ValueError, RecursionError:
        return {"unserializable_result_type": type(value).__name__}


def _clip_text(value: str) -> str:
    if len(value) <= MAX_LIBREFANG_RESPONSE_CHARS:
        return value
    return value[:MAX_LIBREFANG_RESPONSE_CHARS] + "\n[response truncated by AgentOS]"


def _provider_failure(provider: str, error: Exception, *, agent_id: str | None = None) -> dict[str, Any]:
    """Return a useful failure class without leaking response bodies, URLs, or secrets."""
    detail = "request failed; inspect the provider service logs"
    status_code: int | None = None
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        if status_code in {401, 403}:
            detail = "service authentication was rejected; verify the configured credential"
        elif status_code == 404 and agent_id:
            detail = "configured agent was not found; verify LIBREFANG_AGENT_IDS"
        elif status_code == 429:
            detail = "provider rate limit reached"
        else:
            detail = f"provider returned HTTP {status_code}"
    elif isinstance(error, httpx.TimeoutException) or isinstance(error, TimeoutError):
        detail = "provider call timed out"
    elif isinstance(error, ProviderResponseTooLargeError):
        detail = "provider response exceeded the configured body-size limit"
    elif isinstance(error, httpx.RequestError):
        detail = "provider could not be reached"
    else:
        detail = f"provider call failed ({type(error).__name__}); inspect service logs"

    result: dict[str, Any] = {"provider": provider, "status": "failed", "error": detail}
    if agent_id is not None:
        result["agent_id"] = agent_id
    if status_code is not None:
        result["http_status"] = status_code
    return result


async def _run_hatchet(
    config: ParallelExecutionConfig,
    payload: dict[str, Any],
    request_id: str,
    client_factory: Callable[[ParallelExecutionConfig], Any] | None,
) -> dict[str, Any]:
    try:
        client = (client_factory or _hatchet_client)(config)
        if config.hatchet_runnable_kind == "workflow":
            runnable = client.workflow(name=config.hatchet_runnable_name)
        else:
            # Hatchet's standalone task API is a decorator. This local stub
            # creates only the trigger client; the deployed WHOS worker owns execution.
            runnable = client.task(name=config.hatchet_runnable_name)(_remote_standalone_declaration)
        result = await asyncio.wait_for(
            runnable.aio_run(
                input=payload,
                wait_for_result=True,
                child_key=request_id,
                additional_metadata={"source": "whos-agentos", "request_id": request_id},
            ),
            timeout=HATCHET_RESULT_TIMEOUT_SECONDS,
        )
        return {
            "provider": "hatchet",
            "runnable_name": config.hatchet_runnable_name,
            "status": "completed",
            "result": _json_safe(result),
        }
    except Exception as exc:  # provider exceptions must not reveal tokens or request contents
        failure = _provider_failure("hatchet", exc)
        failure["runnable_name"] = config.hatchet_runnable_name
        return failure


async def _run_one_librefang_agent(
    client: httpx.AsyncClient,
    agent_id: str,
    message: str,
    request_id: str,
) -> dict[str, Any]:
    session_id = str(uuid5(NAMESPACE_URL, f"whos-parallel-execution:{request_id}:{agent_id}"))
    path = f"/api/agents/{quote(agent_id, safe='')}/message"
    try:
        async with asyncio.timeout(LIBREFANG_TOTAL_TIMEOUT_SECONDS):
            async with client.stream(
                "POST",
                path,
                json={"message": message, "session_id": session_id},
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length is not None and int(content_length) > MAX_LIBREFANG_RESPONSE_BYTES:
                    raise ProviderResponseTooLargeError
                response_body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=LIBREFANG_STREAM_CHUNK_BYTES):
                    if len(response_body) + len(chunk) > MAX_LIBREFANG_RESPONSE_BYTES:
                        raise ProviderResponseTooLargeError
                    response_body.extend(chunk)
            body = json.loads(response_body)
        if not isinstance(body, dict) or not isinstance(body.get("response"), str):
            raise ValueError("LibreFang returned an invalid message response.")
        result: dict[str, Any] = {
            "provider": "librefang",
            "agent_id": agent_id,
            "session_id": session_id,
            "status": "completed",
            "response": _clip_text(body["response"]),
        }
        for field in ("input_tokens", "output_tokens", "iterations", "cost_usd"):
            if field in body:
                result[field] = body[field]
        return result
    except Exception as exc:  # do not echo response body or bearer credential
        return _provider_failure("librefang", exc, agent_id=agent_id)


async def _run_librefang_agents(
    config: ParallelExecutionConfig,
    message: str,
    request_id: str,
    transport: httpx.AsyncBaseTransport | None,
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {config.librefang_api_key}", "Accept": "application/json"}
    async with httpx.AsyncClient(
        base_url=config.librefang_url,
        headers=headers,
        timeout=LIBREFANG_TIMEOUT_SECONDS,
        follow_redirects=False,
        transport=transport,
    ) as client:
        results = await asyncio.gather(
            *(
                _run_one_librefang_agent(client, agent_id, message, request_id)
                for agent_id in config.librefang_agent_ids
            )
        )
    return results


async def run_parallel_execution(
    *,
    message: str,
    hatchet_payload: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
    config: ParallelExecutionConfig | None = None,
    hatchet_client_factory: Callable[[ParallelExecutionConfig], Any] | None = None,
    httpx_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Dispatch one validated WHOS handoff and fan out a caller-controlled agent prompt.

    The feature flag is checked before payload processing or client construction.
    Missing credentials fail before either provider is called. All network activity
    is confined to the configured Hatchet runnable and 1-3 configured agent IDs.
    """
    settings = config or ParallelExecutionConfig.from_env(environ)
    if not settings.enabled:
        return {
            "status": "disabled",
            "dispatch_attempted": False,
            "reason": f"Set {ENABLE_ENV}=true only after explicit service-activation approval.",
        }

    payload = validate_whos_handoff(hatchet_payload)
    request_id = str(payload["idempotency_key"])
    if not _EXECUTION_SLOT.acquire(blocking=False):
        return {
            "status": "busy",
            "dispatch_attempted": False,
            "reason": "Another parallel-execution fan-out is already active in this AgentOS process.",
        }

    try:
        hatchet_result, librefang_results = await asyncio.gather(
            _run_hatchet(settings, payload, request_id, hatchet_client_factory),
            _run_librefang_agents(settings, message, request_id, httpx_transport),
        )
        successful = hatchet_result.get("status") == "completed" and all(
            result.get("status") == "completed" for result in librefang_results
        )
        return {
            "status": "completed" if successful else "partial_failure",
            "dispatch_attempted": True,
            "request_id": request_id,
            "hatchet": hatchet_result,
            "librefang": librefang_results,
            "automatic_retry": False,
        }
    finally:
        _EXECUTION_SLOT.release()
