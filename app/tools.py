"""
Platform Tools
==============
"""

import asyncio
import threading
from contextlib import suppress
from functools import cache
from os import getenv

from agno.tools.file import FileGenerationTools
from agno.tools.knowledge import KnowledgeManagementTools
from agno.tools.mcp import MCPTools
from agno.tools.openai import OpenAITools
from agno.tools.parallel import ParallelTools
from agno.tools.slack import SlackTools
from agno.utils.log import log_warning

from app.knowledge import product_knowledge

AGNO_DOCS_MCP_URL = "https://docs.agno.com/mcp"
PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
MCP_PROBE_TIMEOUT_SECONDS = 10.0


def _mcp_handshake_ok(url: str) -> bool:
    """Connect, list tools, and close -- once, on a throwaway event loop.

    This is agno's own MCPTools.connect(), the exact path the lifespan takes, so
    "reachable" means a real MCP session came up, not merely that a socket opened.
    A failed connect leaks its anyio cancel scope into whatever loop ran it; here
    that is this private loop, which is discarded, never the app's lifespan.
    """

    async def handshake() -> bool:
        probe = MCPTools(transport="streamable-http", url=url, timeout_seconds=int(MCP_PROBE_TIMEOUT_SECONDS))
        try:
            await asyncio.wait_for(probe.connect(), MCP_PROBE_TIMEOUT_SECONDS)
            return probe.initialized and bool(probe.functions)
        finally:
            with suppress(BaseException):
                await asyncio.wait_for(probe.close(), MCP_PROBE_TIMEOUT_SECONDS)

    with asyncio.Runner() as runner:
        # The leaked scope surfaces again when this loop finalizes the MCP client's
        # async generators, as "Attempted to exit cancel scope in a different task"
        # tracebacks that read exactly like the lifespan crash. The caller already
        # logs the one line that matters, so this private loop stays quiet.
        runner.get_loop().set_exception_handler(lambda _loop, _context: None)
        return runner.run(handshake())


@cache
def _mcp_host_reachable_cached(url: str) -> bool:
    """Does a real MCP session come up at this URL right now?

    Runs in its own thread so it works whether or not the importer already has an
    event loop running (uvicorn imports the app from inside one). Bounded: a
    handshake that has not finished in time counts as unreachable. Cached per
    process, so every component that mounts the toolkit gets one answer.
    """
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["ok"] = _mcp_handshake_ok(url)
        except BaseException as exc:
            outcome["error"] = type(exc).__name__

    thread = threading.Thread(target=run, name=f"mcp-probe:{url}", daemon=True)
    thread.start()
    thread.join(3 * MCP_PROBE_TIMEOUT_SECONDS)
    if thread.is_alive():
        log_warning(f"MCP probe: {url} did not answer within {3 * MCP_PROBE_TIMEOUT_SECONDS:.0f}s")
        return False
    if outcome.get("ok") is True:
        return True
    log_warning(f"MCP probe: {url} unreachable ({outcome.get('error', 'no MCP session or no tools')})")
    return False


class IsolatedMCPTools(MCPTools):
    """MCPTools whose connection lives in a task of its own, never the caller's.

    agno's AgentOS calls connect() from inside the app lifespan. A failed
    streamable-http connect leaks the anyio cancel scope it entered, and that
    scope keeps cancelling the task that entered it: the lifespan task, which
    fails startup or is cancelled later with the job-queue worker stopped while
    HTTP keeps serving. The startup probe cannot prevent that when a host passes
    the probe and fails this connect a moment later.

    Here connect() hands the whole connection to an owner task: the owner enters
    the transport, holds it open until close(), and exits it -- the same task
    throughout, which is what the transport requires. A connect that fails leaks
    into the owner, which then ends; the caller only waits for the outcome and
    is never the scope's host. Tool calls use the session from any task, as they
    already did when the lifespan owned it.
    """

    _owner_task: asyncio.Task[None] | None = None
    _owner_stop: asyncio.Event | None = None

    # MCPTools already overrides the sync Toolkit hooks with coroutines, untyped.
    async def connect(self, force: bool = False) -> None:  # type: ignore[override]
        if force:
            await self.close()
        if self.initialized:
            return
        if self._owner_task is not None and not self._owner_task.done():
            return  # a connect is already in flight, or holding a session open

        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()

        async def own() -> None:
            try:
                await MCPTools.connect(self)
            except BaseException as exc:  # the leaked scope cancels this task, not the caller
                log_warning(f"{self.name} MCP connect failed in its owner task ({type(exc).__name__})")
            finally:
                if not ready.done():
                    ready.set_result(None)
            if not self.initialized:
                return
            try:
                await stop.wait()
            finally:
                with suppress(BaseException):
                    await MCPTools.close(self)

        self._owner_stop = stop
        self._owner_task = asyncio.create_task(own(), name=f"mcp-owner:{self.name}")
        await asyncio.shield(ready)
        if not self.initialized:
            log_warning(f"{self.name} MCP not connected; its tools are unavailable until a later connect succeeds")

    async def close(self) -> None:  # type: ignore[override]
        owner, stop = self._owner_task, self._owner_stop
        self._owner_task = self._owner_stop = None
        if owner is None:
            await MCPTools.close(self)
            return
        if stop is not None:
            stop.set()
        with suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(owner), MCP_PROBE_TIMEOUT_SECONDS)


def get_agno_docs_tools() -> list[MCPTools]:
    """The Agno docs MCP, only when its host answers at startup.

    agno's AgentOS connects every agent's MCPTools inside the app lifespan, and a
    failed streamable-http connect leaks its anyio cancel scope into it: startup
    fails, or the lifespan is cancelled later and takes the job-queue worker with
    it while HTTP keeps serving. agno_docs is optional, so an unreachable host
    means the toolkit is not registered at all; restart to pick it up again.
    A host that passes the probe and fails the lifespan's own connect a moment
    later is contained by IsolatedMCPTools instead: the toolkit stays
    unconnected and the lifespan keeps running.
    """
    if not _mcp_host_reachable_cached(AGNO_DOCS_MCP_URL):
        log_warning("agno_docs MCP skipped: host not reachable at startup, toolkit not registered")
        return []
    return [IsolatedMCPTools(transport="streamable-http", url=AGNO_DOCS_MCP_URL, name="agno_docs")]


def get_parallel_tools() -> list[ParallelTools | MCPTools]:
    if getenv("PARALLEL_API_KEY"):
        return [ParallelTools()]
    # The keyless fallback is an MCP connected in the lifespan, so it gets the same
    # startup probe as agno_docs: an unreachable host must not take the app down.
    if not _mcp_host_reachable_cached(PARALLEL_MCP_URL):
        log_warning("parallel_tools MCP skipped: host not reachable at startup, toolkit not registered")
        return []
    # timeout_seconds: web_fetch page extraction regularly exceeds the 10s MCP default.
    return [
        IsolatedMCPTools(
            url=PARALLEL_MCP_URL,
            transport="streamable-http",
            name="parallel_tools",
            timeout_seconds=30,
        )
    ]


def get_slack_tools() -> list[SlackTools]:
    """Send-scoped Slack toolkit, only when the Slack interface is configured.

    Deliberately narrower than the SlackTools defaults: a registry any agent
    can draw from gets post + channel listing, never history reads or file transfer.
    """
    if not getenv("SLACK_BOT_TOKEN"):
        return []
    return [
        SlackTools(
            token=getenv("SLACK_BOT_TOKEN"),
            enable_send_message=True,
            enable_send_message_thread=True,
            enable_list_channels=True,
            enable_get_channel_history=False,
            enable_upload_file=False,
            enable_download_file=False,
        )
    ]


def get_media_tools() -> list[OpenAITools]:
    """Image generation and text-to-speech on the platform's existing OpenAI key.

    Generated media come back as run artifacts (bytes on the RunResponse), so they
    persist in Postgres and survive ephemeral container filesystems. Transcription
    stays off: transcribe_audio reads server-local file paths, which agents on this
    platform never have.
    """
    # OpenAITools raises without the key; the registry import must not.
    if not getenv("OPENAI_API_KEY"):
        return []
    return [OpenAITools(enable_transcription=False, image_model="gpt-image-2")]


def get_file_generation_tools() -> list[FileGenerationTools]:
    """Downloadable files (JSON, CSV, TXT, HTML, code) as in-memory run artifacts."""
    return [FileGenerationTools(enable_pdf_generation=False, enable_docx_generation=False)]


def get_knowledge_management_tools() -> KnowledgeManagementTools:
    """The write side of the product knowledge base, mounted on Platform Builder."""
    return KnowledgeManagementTools(knowledge=product_knowledge)
