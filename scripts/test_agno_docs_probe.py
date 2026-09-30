"""
Regression tests for the agno_docs MCP startup probe in app/tools.py
====================================================================

agno's AgentOS connects every agent's MCPTools inside the app lifespan. When the
agno_docs host (https://docs.agno.com/mcp) cannot be reached, the streamable-http
client's anyio cancel scope escapes into the lifespan ("Attempted to exit cancel
scope in a different task than it was entered in") and the whole app goes down:
startup fails, or a lifespan that did start is cancelled and stops the job-queue
worker while HTTP keeps answering.

agno_docs is optional, so the platform must boot without it: a host that does not
answer at startup means the toolkit is never registered, and a warning says so.
The keyless Parallel web-search fallback is an MCP in the same lifespan and gets
the same probe.

Hermetic: the MCP host is a local port -- closed, or a stub HTTP server -- and the
product knowledge base is stubbed, so no database and no internet are needed.
agno itself must be importable (CI installs requirements.txt).

Run: python -m unittest discover -s scripts -p "test_*.py"
"""

import http.server
import socket
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# app.knowledge builds PgVector bases at import; the toolkit factories under test
# never touch them, so stand them in rather than require a database.
_knowledge_stub = types.ModuleType("app.knowledge")
_knowledge_stub.product_knowledge = None  # type: ignore[attr-defined]
_knowledge_stub.shared_knowledge = None  # type: ignore[attr-defined]
sys.modules.setdefault("app.knowledge", _knowledge_stub)

from app import tools  # noqa: E402


def _closed_port_url() -> str:
    """A URL on a local port nothing listens on: connection refused, at once."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/mcp"


class _StubHandler(http.server.BaseHTTPRequestHandler):
    status = 405

    def _answer(self) -> None:
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_GET = do_POST = do_DELETE = _answer  # noqa: N815

    def log_message(self, *args: object) -> None:
        pass


class _StubServer:
    """A local plain-HTTP host (not MCP) that answers every request with one status."""

    def __init__(self, status: int):
        handler = type("Handler", (_StubHandler,), {"status": status})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


class _McpServer:
    """A real local MCP server (FastMCP over streamable HTTP) with one tool."""

    def __init__(self) -> None:
        import uvicorn
        from fastmcp import FastMCP

        mcp = FastMCP("probe-stub")

        @mcp.tool
        def ping() -> str:
            return "pong"

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        config = uvicorn.Config(mcp.http_app(path="/mcp"), host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("stub MCP server did not start")
            time.sleep(0.05)
        return f"http://127.0.0.1:{self.port}/mcp"

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)


class AgnoDocsProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        # The probe answer is cached per process; each case starts from none.
        clear = getattr(getattr(tools, "_mcp_host_reachable_cached", None), "cache_clear", None)
        if clear is not None:
            clear()
        # Local stub hosts must not be routed through the sandbox's HTTP proxy.
        env = mock.patch.dict("os.environ", {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        env.start()
        self.addCleanup(env.stop)

    def _toolkits(self, url: str) -> list:
        with mock.patch.object(tools, "AGNO_DOCS_MCP_URL", url):
            return tools.get_agno_docs_tools()

    def test_unreachable_host_is_not_registered(self) -> None:
        with mock.patch.object(tools, "log_warning") as warn:
            self.assertEqual(self._toolkits(_closed_port_url()), [])
        self.assertTrue(warn.called, "skipping agno_docs must log a warning")
        self.assertIn("agno_docs", " ".join(str(c) for c in warn.call_args_list))

    def test_server_error_is_not_registered(self) -> None:
        with _StubServer(503) as url:
            self.assertEqual(self._toolkits(url), [])

    def test_host_that_is_up_but_not_mcp_is_not_registered(self) -> None:
        # A socket that answers is not an MCP session; the lifespan connect would fail.
        with _StubServer(405) as url:
            self.assertEqual(self._toolkits(url), [])

    def test_reachable_mcp_host_is_registered(self) -> None:
        with _McpServer() as url:
            toolkits = self._toolkits(url)
        self.assertEqual([t.name for t in toolkits], ["agno_docs"])
        self.assertEqual(toolkits[0].url, url)

    def test_probe_runs_once_per_process(self) -> None:
        # Platform Builder and the registry both ask; they must get one answer.
        with _McpServer() as url:
            first = self._toolkits(url)
        second = self._toolkits(url)  # server gone: a second probe would say unreachable
        self.assertEqual([t.name for t in first], [t.name for t in second])

    def test_keyless_parallel_mcp_unreachable_is_not_registered(self) -> None:
        # Same lifespan hazard: without PARALLEL_API_KEY web search is an MCP too.
        with (
            mock.patch.dict("os.environ", {"PARALLEL_API_KEY": ""}),
            mock.patch.object(tools, "PARALLEL_MCP_URL", _closed_port_url()),
        ):
            self.assertEqual(tools.get_parallel_tools(), [])

    def test_keyless_parallel_mcp_reachable_is_registered(self) -> None:
        with _McpServer() as url:
            with (
                mock.patch.dict("os.environ", {"PARALLEL_API_KEY": ""}),
                mock.patch.object(tools, "PARALLEL_MCP_URL", url),
            ):
                toolkits = tools.get_parallel_tools()
        self.assertEqual([t.name for t in toolkits], ["parallel_tools"])

    def test_parallel_sdk_needs_no_probe(self) -> None:
        # With a key the SDK is used: no MCP in the lifespan, so nothing to probe.
        with (
            mock.patch.dict("os.environ", {"PARALLEL_API_KEY": "test-key"}),
            mock.patch.object(tools, "PARALLEL_MCP_URL", _closed_port_url()),
        ):
            toolkits = tools.get_parallel_tools()
        self.assertEqual(len(toolkits), 1)
        self.assertNotIsInstance(toolkits[0], tools.MCPTools)

    def test_lifespan_survives_unreachable_host(self) -> None:
        """The reported failure end to end: AgentOS boots and serves /health."""
        from agno.agent import Agent
        from agno.os import AgentOS
        from starlette.testclient import TestClient

        agent = Agent(id="probe-agent", tools=[*self._toolkits(_closed_port_url())])
        app = AgentOS(agents=[agent], telemetry=False, tracing=False).get_app()
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
