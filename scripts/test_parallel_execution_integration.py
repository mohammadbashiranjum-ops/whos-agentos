"""Hermetic tests for the external Parallel Execution integration.

All Hatchet and LibreFang calls are faked; this suite never uses service credentials.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.integrations.parallel_execution import (  # noqa: E402
    ENABLE_ENV,
    MAX_LIBREFANG_RESPONSE_BYTES,
    IntegrationConfigurationError,
    ParallelExecutionConfig,
    _cached_hatchet_client,
    _hatchet_client,
    run_parallel_execution,
    validate_whos_handoff,
)


def _handoff() -> dict[str, Any]:
    return {
        "claim_id": "CLAIM-1",
        "claim_evidence_id": "ISSUE_COMMENT:101",
        "task_id": "TASK-1",
        "command_id": "COMMAND-1",
        "work_unit": "unit-1",
        "lane": "agentos",
        "generation": 1,
        "source_run": "12345",
        "source_head": "b" * 40,
        "write_targets": ["tests/example.py"],
        "traversal_id": "TRAVERSAL-1",
        "idempotency_key": "request-1",
        "owner_acknowledgement_evidence_id": "ISSUE_COMMENT:202",
        "owner_delivery_id": "DELIVERY-1",
        "handoff_digest": "a" * 64,
    }


def _env(**overrides: str) -> dict[str, str]:
    values = {
        ENABLE_ENV: "true",
        "HATCHET_TOKEN": "fake-hatchet-token",
        "HATCHET_URL": "https://hatchet.example.test:7070",
        "HATCHET_RUNNABLE_NAME": "whos-execute-authorized-unit",
        "HATCHET_RUNNABLE_KIND": "standalone",
        "LIBREFANG_URL": "https://librefang.example.test",
        "LIBREFANG_API_KEY": "fake-librefang-key",
        "LIBREFANG_AGENT_IDS": "reader-a,reader-b",
    }
    values.update(overrides)
    return values


class _FakeRunnable:
    def __init__(self, client: _FakeHatchetClient) -> None:
        self.client = client

    async def aio_run(self, **kwargs: Any) -> dict[str, Any]:
        self.client.calls.append(kwargs)
        if self.client.started is not None:
            self.client.started.set()
        if self.client.agent_started is not None:
            await asyncio.wait_for(self.client.agent_started.wait(), timeout=2)
        return {"receipt": "REAL_WORK", "execution_id": "EXEC-1"}


class _FakeHatchetClient:
    def __init__(self, *, started: asyncio.Event | None = None, agent_started: asyncio.Event | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.started = started
        self.agent_started = agent_started
        self.target_name = ""
        self.target_kind = ""

    def workflow(self, *, name: str) -> _FakeRunnable:
        self.target_name = name
        self.target_kind = "workflow"
        return _FakeRunnable(self)

    def task(self, *, name: str):
        self.target_name = name
        self.target_kind = "standalone"
        return lambda _function: _FakeRunnable(self)


class _BarrierTransport(httpx.AsyncBaseTransport):
    def __init__(self, hatchet_started: asyncio.Event) -> None:
        self.hatchet_started = hatchet_started
        self.agent_started = asyncio.Event()
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.agent_started.set()
        await asyncio.wait_for(self.hatchet_started.wait(), timeout=2)
        agent_id = request.url.path.split("/")[3]
        return httpx.Response(
            200,
            json={
                "response": f"read-only result from {agent_id}",
                "input_tokens": 11,
                "output_tokens": 7,
                "iterations": 1,
                "cost_usd": 0.01,
            },
        )


class _RecordingChunkStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, chunk_size: int = 16 * 1024) -> None:
        self.body = body
        self.chunk_size = chunk_size
        self.bytes_yielded = 0

    async def __aiter__(self):
        for offset in range(0, len(self.body), self.chunk_size):
            chunk = self.body[offset : offset + self.chunk_size]
            self.bytes_yielded += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        return None


class _SlowTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, _request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"response": "too late"})


class _BlockingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def handle_async_request(self, _request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return httpx.Response(200, json={"response": "finished"})


class ParallelExecutionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_disabled_by_default_even_when_provider_values_exist(self) -> None:
        values = _env()
        values[ENABLE_ENV] = "false"
        config = ParallelExecutionConfig.from_env(values)
        self.assertFalse(config.enabled)
        self.assertEqual(config.librefang_agent_ids, ())

    async def test_disabled_path_makes_zero_provider_calls(self) -> None:
        called = {"hatchet": 0, "http": 0}

        def hatchet_factory(_config: ParallelExecutionConfig) -> _FakeHatchetClient:
            called["hatchet"] += 1
            return _FakeHatchetClient()

        def http_handler(_request: httpx.Request) -> httpx.Response:
            called["http"] += 1
            return httpx.Response(200, json={"response": "unexpected"})

        result = await run_parallel_execution(
            message="Analyze read-only.",
            hatchet_payload=_handoff(),
            environ={"HATCHET_TOKEN": "present but not activated"},
            hatchet_client_factory=hatchet_factory,
            httpx_transport=httpx.MockTransport(http_handler),
        )
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["dispatch_attempted"])
        self.assertEqual(called, {"hatchet": 0, "http": 0})

    def test_enabled_without_credentials_lists_names_only(self) -> None:
        with self.assertRaises(IntegrationConfigurationError) as caught:
            ParallelExecutionConfig.from_env({ENABLE_ENV: "true", "HATCHET_TOKEN": "do-not-print"})
        self.assertIn("HATCHET_URL", caught.exception.missing)
        self.assertNotIn("do-not-print", str(caught.exception))

    def test_remote_http_and_url_credentials_are_rejected(self) -> None:
        with self.assertRaises(IntegrationConfigurationError):
            ParallelExecutionConfig.from_env(_env(HATCHET_URL="http://hatchet.example.test:7070"))
        with self.assertRaises(IntegrationConfigurationError):
            ParallelExecutionConfig.from_env(_env(LIBREFANG_URL="https://user:pass@librefang.example.test"))

    def test_hatchet_sdk_receives_plain_grpc_target_and_tls_server_name(self) -> None:
        config = ParallelExecutionConfig.from_env(_env())
        _cached_hatchet_client.cache_clear()
        with (
            mock.patch("hatchet_sdk.config.ClientTLSConfig") as tls_config,
            mock.patch("hatchet_sdk.config.ClientConfig") as client_config,
            mock.patch("hatchet_sdk.Hatchet") as hatchet,
        ):
            _hatchet_client(config)
        tls_config.assert_called_once_with(strategy="tls", server_name="hatchet.example.test")
        client_config.assert_called_once_with(
            token="fake-hatchet-token",
            host_port="hatchet.example.test:7070",
            tls_config=tls_config.return_value,
        )
        hatchet.assert_called_once_with(config=client_config.return_value)
        _cached_hatchet_client.cache_clear()

    def test_agent_allowlist_is_bounded_and_unique(self) -> None:
        with self.assertRaises(IntegrationConfigurationError):
            ParallelExecutionConfig.from_env(_env(LIBREFANG_AGENT_IDS="a,b,c,d"))
        with self.assertRaises(IntegrationConfigurationError):
            ParallelExecutionConfig.from_env(_env(LIBREFANG_AGENT_IDS="a,a"))

    def test_handoff_shape_rejects_missing_and_caller_added_fields(self) -> None:
        valid = _handoff()
        self.assertEqual(validate_whos_handoff(valid), valid)
        missing = dict(valid)
        missing.pop("handoff_digest")
        with self.assertRaisesRegex(ValueError, "handoff_digest"):
            validate_whos_handoff(missing)
        extended = {**valid, "arbitrary": "must not be forwarded"}
        with self.assertRaisesRegex(ValueError, "outside the WHOS handoff contract"):
            validate_whos_handoff(extended)

    async def test_hatchet_and_multiple_librefang_agents_run_concurrently(self) -> None:
        hatchet_started = asyncio.Event()
        fake_client = _FakeHatchetClient(started=hatchet_started)
        transport = _BarrierTransport(hatchet_started)

        def hatchet_factory(config: ParallelExecutionConfig) -> _FakeHatchetClient:
            self.assertEqual(config.hatchet_runnable_name, "whos-execute-authorized-unit")
            fake_client.agent_started = transport.agent_started
            return fake_client

        result = await asyncio.wait_for(
            run_parallel_execution(
                message="Read and summarize the unit; do not modify anything.",
                hatchet_payload=_handoff(),
                config=ParallelExecutionConfig.from_env(_env()),
                hatchet_client_factory=hatchet_factory,
                httpx_transport=transport,
            ),
            timeout=3,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["request_id"], "request-1")
        self.assertEqual(result["hatchet"]["result"]["receipt"], "REAL_WORK")
        self.assertEqual(fake_client.target_kind, "standalone")
        self.assertEqual(fake_client.target_name, "whos-execute-authorized-unit")
        self.assertEqual(len(fake_client.calls), 1)
        self.assertTrue(fake_client.calls[0]["wait_for_result"])
        self.assertEqual(fake_client.calls[0]["child_key"], "request-1")
        self.assertEqual(fake_client.calls[0]["input"], _handoff())
        self.assertEqual([item["agent_id"] for item in result["librefang"]], ["reader-a", "reader-b"])
        self.assertTrue(all(item["status"] == "completed" for item in result["librefang"]))
        self.assertEqual(len(transport.requests), 2)
        for request in transport.requests:
            self.assertEqual(request.headers["authorization"], "Bearer fake-librefang-key")
            body = json.loads(request.content)
            self.assertEqual(body["message"], "Read and summarize the unit; do not modify anything.")
            self.assertEqual(request.url.path.split("/")[2], "agents")
            self.assertEqual(request.url.path.split("/")[4], "message")
            self.assertTrue(body["session_id"])

    async def test_provider_failure_is_reported_without_secret_or_cross_branch_cancellation(self) -> None:
        fake_client = _FakeHatchetClient()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("reader-a/message"):
                return httpx.Response(401, json={"error": "not authorized"})
            return httpx.Response(200, json={"response": "reader-b finished", "input_tokens": 1})

        result = await run_parallel_execution(
            message="Inspect safely.",
            hatchet_payload=_handoff(),
            config=ParallelExecutionConfig.from_env(_env()),
            hatchet_client_factory=lambda _config: fake_client,
            httpx_transport=httpx.MockTransport(handler),
        )
        serialized = json.dumps(result)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["hatchet"]["status"], "completed")
        self.assertEqual(result["librefang"][0]["http_status"], 401)
        self.assertEqual(result["librefang"][1]["status"], "completed")
        self.assertNotIn("fake-librefang-key", serialized)
        self.assertNotIn("not authorized", serialized)

    async def test_librefang_response_is_capped_before_full_body_is_read(self) -> None:
        fake_client = _FakeHatchetClient()
        body = b'{"response":"' + (b"x" * (MAX_LIBREFANG_RESPONSE_BYTES * 4)) + b'"}'
        stream = _RecordingChunkStream(body)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream, headers={"content-type": "application/json"})

        result = await run_parallel_execution(
            message="Summarize the authorized unit.",
            hatchet_payload=_handoff(),
            config=ParallelExecutionConfig.from_env(_env()),
            hatchet_client_factory=lambda _config: fake_client,
            httpx_transport=httpx.MockTransport(handler),
        )
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(
            result["librefang"][0]["error"],
            "provider response exceeded the configured body-size limit",
        )
        self.assertLess(stream.bytes_yielded, len(body))

    async def test_librefang_wall_clock_deadline_is_enforced(self) -> None:
        fake_client = _FakeHatchetClient()
        with mock.patch("app.integrations.parallel_execution.LIBREFANG_TOTAL_TIMEOUT_SECONDS", 0.01):
            result = await run_parallel_execution(
                message="Summarize the authorized unit.",
                hatchet_payload=_handoff(),
                config=ParallelExecutionConfig.from_env(_env()),
                hatchet_client_factory=lambda _config: fake_client,
                httpx_transport=_SlowTransport(),
            )
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["librefang"][0]["error"], "provider call timed out")

    async def test_concurrent_run_returns_busy_without_second_provider_calls(self) -> None:
        config = ParallelExecutionConfig.from_env(_env())
        first_transport = _BlockingTransport()
        first_client = _FakeHatchetClient()
        second_calls = {"hatchet": 0, "http": 0}

        first_run = asyncio.create_task(
            run_parallel_execution(
                message="First bounded run.",
                hatchet_payload=_handoff(),
                config=config,
                hatchet_client_factory=lambda _config: first_client,
                httpx_transport=first_transport,
            )
        )
        try:
            await asyncio.wait_for(first_transport.started.wait(), timeout=2)

            def second_factory(_config: ParallelExecutionConfig) -> _FakeHatchetClient:
                second_calls["hatchet"] += 1
                return _FakeHatchetClient()

            def second_handler(_request: httpx.Request) -> httpx.Response:
                second_calls["http"] += 1
                return httpx.Response(200, json={"response": "unexpected second run"})

            second_result = await run_parallel_execution(
                message="Second run should be refused while busy.",
                hatchet_payload=_handoff(),
                config=config,
                hatchet_client_factory=second_factory,
                httpx_transport=httpx.MockTransport(second_handler),
            )
            self.assertEqual(second_result["status"], "busy")
            self.assertFalse(second_result["dispatch_attempted"])
            self.assertEqual(second_calls, {"hatchet": 0, "http": 0})
        finally:
            first_transport.release.set()

        first_result = await asyncio.wait_for(first_run, timeout=3)
        self.assertEqual(first_result["status"], "completed")
        self.assertEqual(first_transport.calls, 2)

    def test_message_schema_and_docs_disclose_no_read_only_enforcement(self) -> None:
        from workflows.parallel_execution import ParallelExecutionInput

        description = ParallelExecutionInput.model_fields["message"].description or ""
        self.assertIn("does not enforce read-only behavior", description)
        docs = (REPO_ROOT / "docs" / "parallel-execution.md").read_text()
        self.assertIn("full agent turn", docs)
        self.assertIn("does not enforce read-only behavior", docs)
        self.assertIn("one active combined execution per AgentOS process", docs)
        self.assertIn("live WHOS service credential's scopes could not be verified", docs)
        self.assertIn("scheme-less gRPC `host:port` target", docs)
        self.assertIn("ClientTLSConfig.server_name", docs)
        self.assertNotIn("The existing WHOS AgentOS service credential has only", docs)

    def test_main_registers_workflow_without_scheduling_it(self) -> None:
        source = (REPO_ROOT / "app" / "main.py").read_text()
        self.assertIn("from workflows.parallel_execution import parallel_execution", source)
        self.assertIn("workflows=[deployment_check, run_evals, parallel_execution]", source)
        schedules = (REPO_ROOT / "app" / "schedules.py").read_text()
        self.assertNotIn("parallel-execution", schedules)


if __name__ == "__main__":
    unittest.main()
