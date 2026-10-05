"""
Regression tests: an MCP connect that fails inside the AgentOS lifespan
======================================================================

The startup probe in app/tools.py (scripts/test_agno_docs_probe.py) keeps an
unreachable host from being registered. It cannot help when a host passes the
probe and then fails the lifespan's own connect a moment later: agno 3.0.4
connects every agent's MCPTools inside the app lifespan, the failed
streamable-http connect leaks its anyio cancel scope into the lifespan task, and
the lifespan is cancelled -- at startup, or later with the job-queue worker
stopped while HTTP keeps answering 200 (whos-backend round-10 r10c-1; PR #10's
reach3 boot).

Two guarantees are pinned here:

1. A lifespan-owned MCP connect that fails cannot cancel the lifespan: the
   connection lives in its own owner task, so a leaked scope cancels that task.
2. /health stops answering 200 once the job-queue worker is dead, so a platform
   that lost its worker is visibly unhealthy instead of silently serving.

Hermetic: every MCP host is local (a real FastMCP server that can be broken on
demand), the product knowledge base is stubbed, no database or internet needed.

Run: python -m unittest discover -s scripts -p "test_*.py"
"""

import asyncio
import socket
import sys
import threading
import time
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Self
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_knowledge_stub = types.ModuleType("app.knowledge")
_knowledge_stub.product_knowledge = None  # type: ignore[attr-defined]
_knowledge_stub.shared_knowledge = None  # type: ignore[attr-defined]
sys.modules.setdefault("app.knowledge", _knowledge_stub)

from app import tools  # noqa: E402


class _BreakableMcpServer:
    """A real local MCP server (FastMCP, streamable HTTP) that can go bad on demand.

    Healthy, it serves one tool. After break_() every request gets 503 -- the host
    that passed the startup probe and then fails the lifespan's own connect.
    """

    def __init__(self) -> None:
        import uvicorn
        from fastmcp import FastMCP

        mcp = FastMCP("breakable-stub")

        @mcp.tool
        def ping() -> str:
            return "pong"

        inner = mcp.http_app(path="/mcp")
        self.broken = False

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] == "http" and self.broken:
                await send({"type": "http.response.start", "status": 503, "headers": [(b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
            await inner(scope, receive, send)

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def break_(self) -> None:
        self.broken = True

    def __enter__(self) -> Self:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("stub MCP server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)


def _boot(agent_tools: list, seconds: float = 3.0) -> dict[str, Any]:
    """Run an AgentOS app's lifespan for `seconds` and report what happened."""
    from agno.agent import Agent
    from agno.os import AgentOS
    from starlette.testclient import TestClient

    seen: dict[str, Any] = {"lifespan_exited_early": False, "health": []}
    serving = {"open": False}

    @asynccontextmanager
    async def lifespan(_app: Any):  # type: ignore[no-untyped-def]
        try:
            yield
        finally:
            if serving["open"]:
                seen["lifespan_exited_early"] = True

    agent = Agent(id="isolation-probe-agent", tools=agent_tools)
    app = AgentOS(agents=[agent], telemetry=False, tracing=False, lifespan=lifespan).get_app()
    try:
        with TestClient(app) as client:
            serving["open"] = True
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                seen["health"].append(client.get("/health").status_code)
                time.sleep(0.5)
            serving["open"] = False
    except BaseException as exc:  # startup failure surfaces here
        seen["startup_error"] = type(exc).__name__
    return seen


class LifespanMcpIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        tools._mcp_host_reachable_cached.cache_clear()
        env = mock.patch.dict("os.environ", {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        env.start()
        self.addCleanup(env.stop)

    def test_host_that_breaks_after_the_probe_does_not_cancel_the_lifespan(self) -> None:
        """The TOCTOU: the probe passes, then the lifespan's own connect gets 503."""
        with _BreakableMcpServer() as server:
            with mock.patch.object(tools, "AGNO_DOCS_MCP_URL", server.url):
                toolkits = tools.get_agno_docs_tools()
            self.assertEqual([t.name for t in toolkits], ["agno_docs"], "the probe must pass first")
            server.break_()
            seen = _boot(toolkits)
        self.assertNotIn("startup_error", seen, f"lifespan failed at startup: {seen}")
        self.assertFalse(seen["lifespan_exited_early"], "lifespan was cancelled while serving")
        self.assertTrue(seen["health"] and all(code == 200 for code in seen["health"]), seen)
        self.assertFalse(toolkits[0].initialized, "a failed connect must not claim a live session")

    def test_healthy_host_connects_in_the_lifespan_and_serves_tool_calls(self) -> None:
        with _BreakableMcpServer() as server:
            with mock.patch.object(tools, "AGNO_DOCS_MCP_URL", server.url):
                toolkit = tools.get_agno_docs_tools()[0]

            async def use() -> tuple[bool, str, bool]:
                await toolkit.connect()
                connected = toolkit.initialized

                # A call from a different task than the one that connected.
                async def call() -> str:
                    assert toolkit.session is not None
                    result = await toolkit.session.call_tool("ping", {})
                    return result.content[0].text  # type: ignore[union-attr]

                answer = await asyncio.create_task(call())
                await toolkit.close()
                return connected, answer, toolkit.initialized

            connected, answer, still_initialized = asyncio.run(use())
        self.assertTrue(connected)
        self.assertEqual(answer, "pong")
        self.assertFalse(still_initialized, "close() must end the session")

    def test_connect_failure_is_contained_outside_any_lifespan(self) -> None:
        """Direct: a failed connect returns normally and cancels nothing around it."""
        with _BreakableMcpServer() as server:
            with mock.patch.object(tools, "AGNO_DOCS_MCP_URL", server.url):
                toolkit = tools.get_agno_docs_tools()[0]
            server.break_()

            async def caller() -> str:
                await toolkit.connect()
                # Give any leaked cancellation time to land on this task.
                for _ in range(20):
                    await asyncio.sleep(0.05)
                return "survived"

            self.assertEqual(asyncio.run(caller()), "survived")
        self.assertFalse(toolkit.initialized)


class _FakeTask:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _FakeWorker:
    def __init__(self, running: bool, task_done: bool | None) -> None:
        self._running = running
        self._task = None if task_done is None else _FakeTask(task_done)


class QueueWorkerHealthTests(unittest.TestCase):
    def _app(self) -> Any:
        from agno.agent import Agent
        from agno.os import AgentOS

        from app.health import install_queue_worker_health

        app = AgentOS(agents=[Agent(id="health-probe-agent")], telemetry=False, tracing=False).get_app()
        install_queue_worker_health(app)
        return app

    def _health(self, worker: object | None) -> tuple[int, dict]:
        from starlette.testclient import TestClient

        app = self._app()
        client = TestClient(app)
        if worker is not None:
            app.state.queue_worker = worker
        response = client.get("/health")
        return response.status_code, response.json()

    def test_live_worker_keeps_health_ok(self) -> None:
        code, body = self._health(_FakeWorker(running=True, task_done=False))
        self.assertEqual(code, 200)
        self.assertEqual(body.get("status"), "ok")

    def test_stopped_worker_fails_health(self) -> None:
        # What agno's lifespan finally leaves behind: worker.stop() ran.
        code, body = self._health(_FakeWorker(running=False, task_done=None))
        self.assertEqual(code, 503)
        self.assertNotEqual(body.get("status"), "ok")

    def test_crashed_worker_task_fails_health(self) -> None:
        code, _ = self._health(_FakeWorker(running=True, task_done=True))
        self.assertEqual(code, 503)

    def test_no_queue_configured_leaves_health_to_agno(self) -> None:
        code, body = self._health(None)
        self.assertEqual(code, 200)
        self.assertEqual(body.get("status"), "ok")

    def _head(self, worker: object | None) -> tuple[int, bytes]:
        from starlette.testclient import TestClient

        app = self._app()
        client = TestClient(app)
        if worker is not None:
            app.state.queue_worker = worker
        response = client.head("/health")
        return response.status_code, response.content

    def test_head_health_answers_like_get(self) -> None:
        # UptimeRobot's default HTTP monitor probes with HEAD. agno's /health is
        # GET-only, so production answered 404 and the monitor read the platform
        # as down every five minutes while GET kept answering 200.
        for worker in (None, _FakeWorker(running=True, task_done=False)):
            code, body = self._head(worker)
            self.assertEqual(code, 200, worker)
            self.assertEqual(body, b"")

    def test_head_health_fails_when_the_worker_is_dead(self) -> None:
        for worker in (_FakeWorker(running=False, task_done=None), _FakeWorker(running=True, task_done=True)):
            code, body = self._head(worker)
            self.assertEqual(code, 503, worker)
            self.assertEqual(body, b"")

    def test_main_app_installs_the_worker_health_check(self) -> None:
        # app.main needs a database to import, so check the wiring in its source.
        source = (REPO_ROOT / "app" / "main.py").read_text()
        self.assertIn("install_queue_worker_health(app)", source)


if __name__ == "__main__":
    unittest.main()
