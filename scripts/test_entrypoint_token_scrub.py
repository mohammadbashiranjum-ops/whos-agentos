"""Regression tests for the token hook in scripts/entrypoint.sh.

The real entrypoint runs under bash with a fake token environment. The process it
`exec`s must not inherit WHOS_WORKSPACE_READ_TOKEN_* (checked from inside that
process, both os.environ and /proc/self/environ), the container must not crash on a
bad pin, and no token may reach stdout or stderr. With WHOS_WORKSPACE_PINS unset the
materialiser does nothing.

Run: python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENTRYPOINT = SCRIPTS / "entrypoint.sh"
APP_ROOT = SCRIPTS.parent
ORG_TOKEN = "ghp_" + "EntryOrgToken0123456789abcdefghij"
USER_TOKEN = "github_pat_" + "EntryUserToken0123456789ABCD"

CHILD = (
    "import os, json;"
    "env_names = sorted(k for k in os.environ if k.upper().startswith('WHOS_WORKSPACE_READ_TOKEN_'));"
    "raw = open('/proc/self/environ','rb').read() if os.path.exists('/proc/self/environ') else b'';"
    "prefix = b'WHOS_WORKSPACE_READ_TOKEN_';"
    "proc_hits = [p.split(b'=')[0].decode() for p in raw.split(b'\\0') if p.upper().startswith(prefix)];"
    "print('CHILD ' + json.dumps({'env': env_names, 'proc': proc_hits, 'pins': 'WHOS_WORKSPACE_PINS' in os.environ}))"
)


@unittest.skipUnless(shutil.which("bash"), "bash is required")
class EntrypointTokenScrubTests(unittest.TestCase):
    def setUp(self):
        self.bin = Path(tempfile.mkdtemp(prefix="ep-bin-"))
        self.addCleanup(shutil.rmtree, self.bin, True)
        (self.bin / "python").symlink_to(sys.executable)
        # The entrypoint's default root is <repo>/workspaces; remember whether it existed so the
        # default-off assertion is about this run and the directory is removed again afterwards.
        self.workspaces = APP_ROOT / "workspaces"
        self.workspaces_existed = self.workspaces.exists()
        if not self.workspaces_existed:
            self.addCleanup(lambda: shutil.rmtree(self.workspaces, ignore_errors=True))

    def run_entrypoint(self, extra_env, command=None, path_dir=None):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("WHOS_WORKSPACE_")}
        env["PATH"] = f"{path_dir or self.bin}:{env.get('PATH', '')}"
        env.pop("WAIT_FOR_DB", None)
        env.update(extra_env)
        proc = subprocess.run(
            ["bash", str(ENTRYPOINT), *(command or [sys.executable, "-c", CHILD])],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(APP_ROOT),
        )
        return proc

    @staticmethod
    def child_report(stdout):
        line = next(ln for ln in stdout.splitlines() if ln.startswith("CHILD "))
        return json.loads(line[len("CHILD ") :])

    def assert_no_token(self, proc):
        for token in (ORG_TOKEN, USER_TOKEN):
            self.assertNotIn(token, proc.stdout + proc.stderr)

    def test_default_off_skips_and_still_scrubs_the_child_environment(self):
        proc = self.run_entrypoint(
            {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN, "WHOS_WORKSPACE_READ_TOKEN_USER": USER_TOKEN}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WORKSPACE_SKIPPED reason=unset", proc.stdout)
        self.assertIn("TOKEN_SCRUBBED scope=entrypoint removed=2", proc.stdout)
        report = self.child_report(proc.stdout)
        self.assertEqual(report, {"env": [], "proc": [], "pins": False})
        self.assert_no_token(proc)
        if not self.workspaces_existed:
            self.assertFalse(self.workspaces.exists(), "default off must not create workspaces/")

    def test_a_name_a_shell_cannot_unset_is_still_removed_from_the_exec_target(self):
        odd = "WHOS_WORKSPACE_READ_TOKEN_A.B"
        proc = self.run_entrypoint({odd: ORG_TOKEN, "WHOS_WORKSPACE_READ_TOKEN_ORG": USER_TOKEN})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("TOKEN_SCRUBBED scope=entrypoint removed=2", proc.stdout)
        report = self.child_report(proc.stdout)
        self.assertEqual(report["env"], [])
        self.assertEqual(report["proc"], [])
        self.assert_no_token(proc)

    def test_no_tokens_set_prints_no_scrub_line(self):
        proc = self.run_entrypoint({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WORKSPACE_SKIPPED reason=unset", proc.stdout)
        self.assertNotIn("TOKEN_SCRUBBED", proc.stdout)

    def test_bad_pins_do_not_crash_the_container_and_tokens_are_still_removed(self):
        proc = self.run_entrypoint(
            {
                "WHOS_WORKSPACE_PINS": "{ this is not json",
                "WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN,
                "WHOS_WORKSPACE_READ_TOKEN_USER": USER_TOKEN,
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WORKSPACE_REFUSED repo=* reason=pins_invalid", proc.stdout)
        self.assertIn("TOKEN_SCRUBBED removed=2", proc.stdout)
        self.assertEqual(self.child_report(proc.stdout)["env"], [])
        self.assertEqual(self.child_report(proc.stdout)["proc"], [])
        self.assert_no_token(proc)

    def test_a_materialiser_that_dies_is_reported_and_tokens_are_still_removed(self):
        shim_dir = Path(tempfile.mkdtemp(prefix="ep-shim-"))
        self.addCleanup(shutil.rmtree, shim_dir, True)
        shim = shim_dir / "python"
        shim.write_text('#!/bin/sh\necho "boom $WHOS_WORKSPACE_READ_TOKEN_ORG" >&2\nexit 3\n')
        shim.chmod(0o755)
        proc = self.run_entrypoint({"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN}, path_dir=shim_dir)
        # The shim itself echoed the token to stderr: that is the shim's doing, not the hook's.
        self.assertEqual(proc.returncode, 0)
        self.assertIn("WORKSPACE_REFUSED repo=* reason=materialiser_failed", proc.stdout)
        self.assertIn("TOKEN_SCRUBBED scope=entrypoint removed=1", proc.stdout)
        self.assertEqual(self.child_report(proc.stdout)["env"], [])
        self.assertNotIn(ORG_TOKEN, proc.stdout)

    def test_no_python_at_all_does_not_stop_the_container(self):
        bare = Path(tempfile.mkdtemp(prefix="ep-bare-"))
        self.addCleanup(shutil.rmtree, bare, True)
        for tool in ("bash", "cat", "dirname", "grep"):
            found = shutil.which(tool)
            if found is None:
                self.skipTest(f"{tool} not found")
            (bare / tool).symlink_to(found)
        env = {"PATH": str(bare), "WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN}
        proc = subprocess.run(
            [str(bare / "bash"), str(ENTRYPOINT), sys.executable, "-c", CHILD],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WORKSPACE_REFUSED repo=* reason=materialiser_failed", proc.stdout)
        self.assertEqual(self.child_report(proc.stdout)["env"], [])
        self.assertNotIn(ORG_TOKEN, proc.stdout + proc.stderr)

    def test_arguments_with_spaces_survive_the_hook(self):
        proc = self.run_entrypoint(
            {}, command=[sys.executable, "-c", "import sys; print('ARGV', sys.argv[1])", "a b  c"]
        )
        self.assertIn("ARGV a b  c", proc.stdout)

    def test_chill_mode_scrub_runs_before_the_case(self):
        text = ENTRYPOINT.read_text()
        # The boot block moved to the shared scripts/workspace_boot.sh (sourced before the case).
        boot = (SCRIPTS / "workspace_boot.sh").read_text()
        self.assertLess(text.index('source "${SCRIPT_DIR}/workspace_boot.sh"'), text.index('case "$1" in'))
        self.assertLess(boot.index("materialise_workspaces.py"), boot.index("TOKEN_SCRUBBED"))


if __name__ == "__main__":
    unittest.main()
