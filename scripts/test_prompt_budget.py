"""Tests for app/prompt_budget.py and the PEE Reader prompt budget (agents/pee_reader.py).

No network, no model: the agent is built for real (agno 3.0.4) and its tools are called directly.
`WorstCaseBudgetTests` is the permanent guard: it fails if instructions, tool schemas, caps or the
completion limit grow past what a free lane (8,000 TPM / 7,000 ITPM) accepts in one request.

Run: python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agno.tools.workspace import Workspace  # noqa: E402

from agents import pee_reader  # noqa: E402
from app import prompt_budget as pb  # noqa: E402
from app.settings import default_model  # noqa: E402

CAP = pb.DEFAULT_TOOL_RESULT_MAX_TOKENS
TOOL_CALL_MESSAGE_TOKENS = 100  # a resent assistant tool-call message (name + short args)
BASE_FLOOR_TOKENS = 1600  # measured: system prompt + workspace instructions + tool schemas, resent every step


class CapTextTests(unittest.TestCase):
    def test_under_cap_is_unchanged(self):
        self.assertEqual(pb.cap_text("a\nb\nc", 10), ("a\nb\nc", False))
        text = "x" * (10 * pb.CHARS_PER_TOKEN)
        self.assertEqual(pb.cap_text(text, 10), (text, False))

    def test_over_cap_ends_with_marker_within_cap_on_a_line_boundary(self):
        text = "".join(f"line {i}\n" for i in range(1, 2001))
        out, truncated = pb.cap_text(text, 300)
        self.assertTrue(truncated)
        self.assertLessEqual(len(out), 300 * pb.CHARS_PER_TOKEN)
        self.assertLessEqual(pb.estimate_tokens(out), 300)
        head, marker, _ = out.partition("\n[TRUNCATED: showing ")
        self.assertTrue(marker)
        self.assertTrue(out.endswith("narrow the range or path and call again]"))
        self.assertTrue(text.startswith(head + "\n"), "cut on a line boundary, head is a whole-line prefix")
        shown = int(out.split("showing ")[1].split(" of ")[0])
        self.assertEqual(shown, head.count("\n") + 1)
        self.assertIn(f"of {text.count(chr(10)) + 1} lines", out)

    def test_idempotent(self):
        out, _ = pb.cap_text("word\n" * 5000, 200)
        self.assertEqual(pb.cap_text(out, 200), (out, False))

    def test_one_huge_line_is_hard_cut_and_still_capped(self):
        out, truncated = pb.cap_text("y" * 50_000, 250)
        self.assertTrue(truncated)
        self.assertLessEqual(len(out), 250 * pb.CHARS_PER_TOKEN)

    def test_non_string_is_returned_unchanged_and_tiny_cap_never_raises(self):
        for value in (None, 5, ["a"], b"bytes"):
            self.assertEqual(pb.cap_text(value, 10), (value, False))
        out, truncated = pb.cap_text("abc\n" * 100, 1)
        self.assertTrue(truncated)
        self.assertLessEqual(len(out), pb.CHARS_PER_TOKEN)

    def test_estimate_is_chars_over_three_rounded_up(self):
        self.assertEqual(pb.estimate_tokens(""), 0)
        self.assertEqual(pb.estimate_tokens("abc"), 1)
        self.assertEqual(pb.estimate_tokens("abcd"), 2)


class EnvClampTests(unittest.TestCase):
    CASES = (
        (pb.TOOL_RESULT_MAX_TOKENS_ENV, pb.tool_result_max_tokens, 700, (200, 3000)),
        (pb.TOOL_CALL_LIMIT_ENV, pb.tool_call_limit, 3, (1, 8)),
        (pb.MAX_COMPLETION_TOKENS_ENV, pb.max_completion_tokens, 2048, (128, 4096)),
    )

    def test_defaults_when_unset_empty_or_garbage(self):
        for name, reader, default, _ in self.CASES:
            for raw in (None, "", "  ", "abc", "1.5", "-5", "0"):
                env = {k: v for k, v in os.environ.items() if k != name}
                if raw is not None:
                    env[name] = raw
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(reader(), default, (name, raw))

    def test_values_are_clamped_and_valid_values_pass_through(self):
        for name, reader, default, (lo, hi) in self.CASES:
            for raw, expected in (("1", lo), ("99999", hi), (str(lo), lo), (str(hi), hi), (f" {default + 1} ", None)):
                with mock.patch.dict(os.environ, {name: raw}):
                    want = min(max(default + 1, lo), hi) if expected is None else expected
                    self.assertEqual(reader(), want, (name, raw))


def _tools(env: dict | None = None):
    """Build the agent's tool list fresh (env is read at build time) over a throw-away tree."""
    root = Path(tempfile.mkdtemp(prefix="pee-budget-"))
    ws = Workspace(root=root, allowed=Workspace.READ_TOOLS, exclude_patterns=list(pee_reader.EXCLUDE_PATTERNS))
    with mock.patch.dict(os.environ, env or {}), mock.patch.object(pee_reader.workspace, "get_tools", lambda: [ws]):
        built = pee_reader._build_tools()
    return root, ws, built


class ToolCapTests(unittest.TestCase):
    def setUp(self):
        self.root, self.ws, built = _tools()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.reader = built[1]
        (self.root / "src").mkdir()
        for i in range(500):
            (self.root / "src" / f"file_{i:03d}.ts").write_text(f"export const v{i} = 'needle';\n" + "pad " * 400)
        (self.root / "big.ts").write_text("".join(f"line {i} " + "z" * 60 + "\n" for i in range(1, 1001)))
        self.limit_chars = CAP * pb.CHARS_PER_TOKEN

    def _fn(self, name: str, is_async: bool):
        return (self.ws.async_functions if is_async else self.ws.functions)[name].entrypoint

    def _call(self, name: str, is_async: bool, **kwargs):
        fn = self._fn(name, is_async)
        return asyncio.run(fn(**kwargs)) if is_async else fn(**kwargs)

    def test_list_files_with_500_files_is_capped_sync_and_async(self):
        for is_async in (False, True):
            out = self._call("list_files", is_async, directory="src", recursive=True, max_depth=9)
            with self.subTest(is_async=is_async):
                self.assertLessEqual(len(out), self.limit_chars)
                data = json.loads(out)
                self.assertEqual(len(data["files"]), pee_reader.LIST_FILES_MAX_ENTRIES)
                self.assertEqual(data["entries_total"], 500)
                self.assertIn("truncated", data)

    def test_list_files_depth_is_clamped(self):
        (self.root / "a" / "b" / "c" / "d").mkdir(parents=True)
        (self.root / "a" / "b" / "c" / "d" / "deep.ts").write_text("x")
        out = self._call("list_files", False, directory="a", recursive=True, max_depth=50)
        self.assertNotIn("deep.ts", out)
        self.assertIn("a/b/c", out)

    def test_small_listing_is_untouched(self):
        (self.root / "tiny").mkdir()
        (self.root / "tiny" / "one.ts").write_text("x")
        raw = self.ws.list_files("tiny")
        self.assertEqual(self._call("list_files", False, directory="tiny"), raw)
        self.assertEqual(self._call("list_files", True, directory="tiny"), raw)

    def test_search_content_is_capped_sync_and_async(self):
        for is_async in (False, True):
            out = self._call("search_content", is_async, query="needle", directory="src", limit=500)
            with self.subTest(is_async=is_async):
                self.assertLessEqual(len(out), self.limit_chars)
                self.assertIn("[TRUNCATED", out)

    def test_read_lines_is_capped_sync_and_async_and_keeps_line_one(self):
        sync = self.reader.read_lines("big.ts", 1, 200)
        asynchronous = asyncio.run(self.reader.aread_lines("big.ts", 1, 200))
        for out in (sync, asynchronous):
            self.assertLessEqual(len(out), self.limit_chars)
            self.assertTrue(out.startswith("     1\tline 1 "))
            self.assertIn("[TRUNCATED: showing ", out)

    def test_max_read_chars_is_cap_derived(self):
        self.assertEqual(pee_reader.MAX_READ_CHARS, pb.tool_result_max_tokens() * pb.CHARS_PER_TOKEN)

    def test_wrapped_tools_keep_their_model_facing_schema(self):
        for name, params in (
            ("list_files", {"directory", "pattern", "recursive", "max_depth"}),
            ("search_content", {"query", "directory", "limit"}),
        ):
            for is_async in (False, True):
                function = (self.ws.async_functions if is_async else self.ws.functions)[name]
                function.process_entrypoint()
                self.assertEqual(set(function.parameters["properties"]), params, (name, is_async))
                self.assertTrue(function.description)

    def test_env_raises_the_cap(self):
        root, ws, built = _tools({pb.TOOL_RESULT_MAX_TOKENS_ENV: "3000"})
        self.addCleanup(shutil.rmtree, root, True)
        (root / "w.ts").write_text("".join(f"l{i} " + "q" * 60 + "\n" for i in range(1, 200)))
        out = built[1].read_lines("w.ts", 1, 200)
        self.assertGreater(len(out), self.limit_chars)
        self.assertLessEqual(len(out), 3000 * pb.CHARS_PER_TOKEN)


def _completion_cap(model: Any) -> int | None:
    """OpenAILike carries max_completion_tokens; OpenAIResponses (no provider key set) carries max_output_tokens."""
    return getattr(model, "max_completion_tokens", None) or getattr(model, "max_output_tokens", None)


class AgentWiringTests(unittest.TestCase):
    def test_defaults_on_the_built_agent(self):
        agent = pee_reader.pee_reader
        self.assertEqual(agent.tool_call_limit, pb.tool_call_limit())
        self.assertEqual(_completion_cap(agent.model), pb.max_completion_tokens())

    def test_default_model_sets_the_completion_cap_only_when_asked(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}, clear=True):
            self.assertIsNone(_completion_cap(default_model()))
            self.assertEqual(_completion_cap(default_model(max_completion_tokens=1024)), 1024)
        with mock.patch.dict(os.environ, {"WHOS_FUEL_BASE_URL": "http://x/v1"}, clear=True):
            self.assertEqual(_completion_cap(default_model(max_completion_tokens=512)), 512)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}, clear=True):
            self.assertEqual(_completion_cap(default_model(max_completion_tokens=512)), 512)
            self.assertIsNone(_completion_cap(default_model()))

    def test_other_agents_are_not_capped(self):
        from agents import engineer

        self.assertIsNone(_completion_cap(engineer.platform_engineer.model))
        self.assertIsNone(engineer.platform_engineer.tool_call_limit)

    def test_still_no_offload_store(self):
        self.assertIsNone(pee_reader.pee_reader.offload_tool_results)


class WorstCaseBudgetTests(unittest.TestCase):
    """Permanent guard: base + every allowed tool call at its cap + the completions must fit one free-lane request."""

    def test_defaults_fit_the_request_budget(self):
        agent: Any = pee_reader.pee_reader
        tool_schemas = []
        for toolkit in agent.tools:
            for function in toolkit.functions.values():
                function.process_entrypoint()
                tool_schemas.append(function.to_dict())
        base = pb.estimate_tokens(str(agent.instructions)) + pb.estimate_tokens(json.dumps(tool_schemas))
        self.assertLessEqual(base, BASE_FLOOR_TOKENS + 400, f"base prompt grew to ~{base} tokens")
        calls, cap, completion = int(agent.tool_call_limit), pb.tool_result_max_tokens(), pb.max_completion_tokens()
        # Earlier assistant turns are short tool-call messages that are resent; the last completion is only requested.
        worst = base + calls * cap + calls * TOOL_CALL_MESSAGE_TOKENS + completion
        self.assertLessEqual(worst, pb.REQUEST_BUDGET_TOKENS, f"worst-case request ~{worst} tokens")

    def test_the_documented_defaults_themselves_fit(self):
        worst = (
            BASE_FLOOR_TOKENS
            + pb.DEFAULT_TOOL_CALL_LIMIT * pb.DEFAULT_TOOL_RESULT_MAX_TOKENS
            + pb.DEFAULT_TOOL_CALL_LIMIT * TOOL_CALL_MESSAGE_TOKENS
            + pb.DEFAULT_MAX_COMPLETION_TOKENS
        )
        self.assertLessEqual(worst, pb.REQUEST_BUDGET_TOKENS)

    def test_a_default_run_is_not_the_clamp_ceiling(self):
        ceiling = pb.TOOL_CALL_LIMIT_RANGE[1] * pb.TOOL_RESULT_MAX_TOKENS_RANGE[1]
        self.assertGreater(ceiling, pb.REQUEST_BUDGET_TOKENS, "raising the knobs is an explicit, deliberate act")


if __name__ == "__main__":
    unittest.main()
