"""Regression tests for app/token_guard.py (TOKEN_ENV_CLEAN / TOKEN_ENV_LEAK).

Pinned here (design 2.5): a process that still carries a
WHOS_WORKSPACE_READ_TOKEN_* variable deletes it, says so by NAME only, and exits
non-zero before the app object is built; a clean process logs TOKEN_ENV_CLEAN.

Run: python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import token_guard  # noqa: E402

SNIPPET = (
    "import sys; sys.path.insert(0, " + repr(str(REPO_ROOT)) + "); "
    "from app.token_guard import enforce_clean_token_env as e; e(); print('SERVED')"
)
TOKEN = "ghp_" + "GuardTokenValue0123456789abcdefgh"


class Exit(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _exit(code):
    raise Exit(code)


class TokenGuardTests(unittest.TestCase):
    def run_guard(self, environ):
        out, err = io.StringIO(), io.StringIO()
        code = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                token_guard.enforce_clean_token_env(environ, exit_fn=_exit)
            except Exit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_clean_environment(self):
        code, out, _ = self.run_guard({"PATH": "/bin", "WHOS_WORKSPACE_PINS": "{}"})
        self.assertIsNone(code)
        self.assertEqual(out, "TOKEN_ENV_CLEAN\n")

    def test_leak_deletes_the_variables_and_exits_non_zero(self):
        environ = {
            "WHOS_WORKSPACE_READ_TOKEN_ORG": TOKEN,
            "WHOS_WORKSPACE_READ_TOKEN_USER": TOKEN[::-1],
            "KEEP": "1",
        }
        code, out, err = self.run_guard(environ)
        self.assertNotEqual(code, 0)
        self.assertEqual(code, token_guard.LEAK_EXIT_CODE)
        self.assertIn("TOKEN_ENV_LEAK count=2", out)
        self.assertIn("WHOS_WORKSPACE_READ_TOKEN_ORG", out)
        self.assertNotIn("TOKEN_ENV_CLEAN", out)
        self.assertNotIn(TOKEN, out + err)
        self.assertNotIn(TOKEN[::-1], out + err)
        self.assertEqual(environ, {"KEEP": "1"})

    def test_lower_case_spelling_is_a_leak_too(self):
        environ = {"whos_workspace_read_token_org": TOKEN}
        code, out, _ = self.run_guard(environ)
        self.assertEqual(code, token_guard.LEAK_EXIT_CODE)
        self.assertEqual(environ, {})

    def test_similar_names_are_not_tokens(self):
        code, out, _ = self.run_guard(
            {"WHOS_WORKSPACE_PINS": "x", "WHOS_WORKSPACE_READ": "x", "OTHER_WHOS_WORKSPACE_READ_TOKEN_ORG": "x"}
        )
        self.assertIsNone(code)

    def test_a_real_process_with_a_leaked_token_exits_non_zero(self):
        env = {**os.environ, "WHOS_WORKSPACE_READ_TOKEN_ORG": TOKEN}
        proc = subprocess.run(
            [sys.executable, "-I", "-c", SNIPPET],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, token_guard.LEAK_EXIT_CODE)
        self.assertIn("TOKEN_ENV_LEAK", proc.stdout)
        self.assertNotIn("SERVED", proc.stdout)
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)

    def test_a_real_clean_process_continues(self):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("WHOS_WORKSPACE_READ_TOKEN_")}
        proc = subprocess.run(
            [sys.executable, "-I", "-c", SNIPPET],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.split(), ["TOKEN_ENV_CLEAN", "SERVED"])


class MainRunsTheGuardFirstTests(unittest.TestCase):
    """app/main.py must fail closed before anything else is built (main needs a database, so: static + real process)."""

    def test_the_guard_import_is_the_first_import_in_main(self):
        tree = ast.parse((REPO_ROOT / "app" / "main.py").read_text())
        imports = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
        first = imports[0]
        self.assertIsInstance(first, ast.ImportFrom)
        self.assertEqual(first.module, "app")
        self.assertEqual([a.name for a in first.names], ["token_guard_check"])

    def test_importing_main_with_a_leaked_token_exits_before_the_database_is_touched(self):
        env = {**os.environ, "WHOS_WORKSPACE_READ_TOKEN_ORG": TOKEN, "RUNTIME_ENV": "dev"}
        code = "import sys; sys.path.insert(0, " + repr(str(REPO_ROOT)) + "); import app.main"
        proc = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, token_guard.LEAK_EXIT_CODE, proc.stderr[-400:])
        self.assertIn("TOKEN_ENV_LEAK", proc.stdout)
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)

    def test_pee_reader_is_registered(self):
        source = (REPO_ROOT / "app" / "main.py").read_text()
        self.assertIn("pee_reader", source.split("agents=[", 1)[1].split("]", 1)[0])


if __name__ == "__main__":
    unittest.main()
