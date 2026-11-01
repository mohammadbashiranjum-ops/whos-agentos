"""Regression tests for the uvicorn PATH shim and the shared workspace boot (WHOS PEE phase 1, rev 3).

Railway runs a startCommand INSTEAD of the Dockerfile ENTRYPOINT, and the live service override is a
bare `uvicorn app.main:app --host 0.0.0.0 --port 8000`. The Dockerfile therefore puts
scripts/shim FIRST on PATH; the shim runs scripts/workspace_boot.sh (materialiser call and token
scrub, shared with scripts/entrypoint.sh) and then execs the real uvicorn without any token.

The real shim runs here with the live command shape; the "real" uvicorn is a stub that reports its
arguments and what it can see of the environment (os.environ and /proc/self/environ).

Run: python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
APP_ROOT = SCRIPTS.parent
SHIM_DIR = SCRIPTS / "shim"
SHIM = SHIM_DIR / "uvicorn"
ENTRYPOINT = SCRIPTS / "entrypoint.sh"
ARGS = ["app.main:app", "--host", "0.0.0.0", "--port", "8000"]
ORG_TOKEN = "ghp_" + "ShimOrgToken0123456789abcdefghijk"
ODD_TOKEN = "ghp_" + "ShimOddToken0123456789abcdefghijk"
ODD_NAME = "WHOS_WORKSPACE_READ_TOKEN_A.B"

STUB = """#!{python}
import os, sys
names = sorted(k for k in os.environ if k.upper().startswith("WHOS_WORKSPACE_READ_TOKEN_"))
raw = open("/proc/self/environ", "rb").read() if os.path.exists("/proc/self/environ") else b""
proc = [p.split(b"=")[0].decode() for p in raw.split(b"\\0") if p.upper().startswith(b"WHOS_WORKSPACE_READ_TOKEN_")]
print("STUB_ARGS " + repr(sys.argv[1:]))
print("STUB_ENV env=%r proc=%r" % (names, proc))
sys.exit(7)
"""


class ShimFilesTests(unittest.TestCase):
    def test_shim_and_boot_files_are_executable_and_dockerfile_orders_path(self):
        self.assertTrue(os.access(SHIM, os.X_OK))
        self.assertTrue(os.access(SCRIPTS / "workspace_boot.sh", os.X_OK))
        dockerfile = (APP_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("chmod +x /app/scripts/entrypoint.sh /app/scripts/shim/uvicorn", dockerfile)
        match = re.search(r'^ENV PATH="([^"]+)"$', dockerfile, re.M)
        self.assertIsNotNone(match, "Dockerfile must set ENV PATH")
        self.assertTrue(match.group(1).startswith("/app/scripts/shim:"), match.group(1))
        self.assertIn("${PATH}", match.group(1))
        self.assertIn('ENTRYPOINT ["/app/scripts/entrypoint.sh"]', dockerfile)

    def test_entrypoint_and_shim_share_one_boot_script(self):
        self.assertIn("workspace_boot.sh", ENTRYPOINT.read_text(encoding="utf-8"))
        self.assertIn("workspace_boot.sh", SHIM.read_text(encoding="utf-8"))
        self.assertNotIn("materialise_workspaces.py", ENTRYPOINT.read_text(encoding="utf-8"))


@unittest.skipUnless(shutil.which("bash") and os.path.exists("/proc/self/environ"), "bash and /proc are required")
class UvicornShimTests(unittest.TestCase):
    def setUp(self):
        self.real = Path(tempfile.mkdtemp(prefix="shim-real-"))
        self.addCleanup(shutil.rmtree, self.real, True)
        (self.real / "python").symlink_to(sys.executable)
        stub = self.real / "uvicorn"
        stub.write_text(STUB.format(python=sys.executable), encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.workspaces = APP_ROOT / "workspaces"
        if not self.workspaces.exists():
            self.addCleanup(lambda: shutil.rmtree(self.workspaces, ignore_errors=True))

    def env(self, extra=None, path=None):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("WHOS_")}
        env["PATH"] = path or f"{SHIM_DIR}:{self.real}:{env.get('PATH', '')}"
        env.pop("WAIT_FOR_DB", None)
        env.update(extra or {})
        return env

    def run_cmd(self, argv, extra=None, path=None):
        return subprocess.run(
            argv, env=self.env(extra, path), capture_output=True, text=True, timeout=120, cwd=str(APP_ROOT)
        )

    def test_path_resolves_uvicorn_to_the_shim_first(self):
        proc = self.run_cmd(["bash", "-c", "command -v uvicorn"])
        self.assertEqual(proc.stdout.strip(), str(SHIM))

    def test_live_command_shape_pins_unset(self):
        proc = self.run_cmd(["bash", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000"])
        self.assertEqual(proc.returncode, 7, proc.stdout + proc.stderr)
        self.assertIn("WORKSPACE_SKIPPED reason=unset", proc.stdout)
        self.assertIn(f"STUB_ARGS {ARGS!r}", proc.stdout)
        self.assertIn("STUB_ENV env=[] proc=[]", proc.stdout)

    def test_argument_with_space_stays_one_argument(self):
        proc = self.run_cmd(["uvicorn", "a b", "--x=1"])
        self.assertIn("STUB_ARGS ['a b', '--x=1']", proc.stdout)

    def test_token_is_scrubbed_and_never_printed(self):
        proc = self.run_cmd(["uvicorn", *ARGS], {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN})
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 7, out)
        self.assertNotIn(ORG_TOKEN, out)
        self.assertIn("TOKEN_SCRUBBED scope=entrypoint removed=1", proc.stdout)
        self.assertIn("STUB_ENV env=[] proc=[]", proc.stdout)

    def test_shell_invalid_token_name_is_removed_with_env_u(self):
        proc = self.run_cmd(["uvicorn", *ARGS], {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN, ODD_NAME: ODD_TOKEN})
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 7, out)
        self.assertNotIn(ORG_TOKEN, out)
        self.assertNotIn(ODD_TOKEN, out)
        self.assertIn("TOKEN_SCRUBBED scope=entrypoint removed=2", proc.stdout)
        self.assertIn("STUB_ENV env=[] proc=[]", proc.stdout)

    def test_entrypoint_then_shim_materialises_once(self):
        proc = self.run_cmd(["bash", str(ENTRYPOINT), "uvicorn", *ARGS], {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN})
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 7, out)
        self.assertEqual(proc.stdout.count("WORKSPACE_SKIPPED"), 1, out)
        self.assertEqual(proc.stdout.count("TOKEN_SCRUBBED"), 1, out)
        self.assertNotIn(ORG_TOKEN, out)
        self.assertIn(f"STUB_ARGS {ARGS!r}", proc.stdout)
        self.assertIn("STUB_ENV env=[] proc=[]", proc.stdout)

    def test_boot_marker_skips_materialiser_but_not_the_scrub(self):
        proc = self.run_cmd(
            ["uvicorn", *ARGS], {"WHOS_WORKSPACE_BOOT_DONE": "1", "WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN}
        )
        self.assertNotIn("WORKSPACE_SKIPPED", proc.stdout)
        self.assertNotIn(ORG_TOKEN, proc.stdout + proc.stderr)
        self.assertIn("STUB_ENV env=[] proc=[]", proc.stdout)

    def test_missing_real_uvicorn_fails_loudly(self):
        if any(os.access(f"{d}/uvicorn", os.X_OK) for d in ("/usr/bin", "/bin")):
            self.skipTest("a real uvicorn is on the system PATH")
        proc = self.run_cmd([str(SHIM), *ARGS], path=f"{SHIM_DIR}:/usr/bin:/bin")
        self.assertEqual(proc.returncode, 127)
        self.assertIn("UVICORN_SHIM_FAILED reason=real_uvicorn_not_found", proc.stderr)
        self.assertNotIn("STUB_ARGS", proc.stdout)

    def test_shim_never_selects_itself(self):
        # Only the shim directory carries a `uvicorn`: it must refuse, not exec itself in a loop.
        if any(os.access(f"{d}/uvicorn", os.X_OK) for d in ("/usr/bin", "/bin")):
            self.skipTest("a real uvicorn is on the system PATH")
        proc = self.run_cmd(["uvicorn", *ARGS], path=f"{SHIM_DIR}:{SHIM_DIR}/../shim:/usr/bin:/bin")
        self.assertEqual(proc.returncode, 127)

    def test_symlink_of_the_shim_in_another_path_dir_is_skipped(self):
        other = Path(tempfile.mkdtemp(prefix="shim-link-"))
        self.addCleanup(shutil.rmtree, other, True)
        (other / "uvicorn").symlink_to(SHIM)
        proc = self.run_cmd(["uvicorn", *ARGS], path=f"{SHIM_DIR}:{other}:{self.real}:/usr/bin:/bin")
        self.assertEqual(proc.returncode, 7, proc.stdout + proc.stderr)
        self.assertIn(f"STUB_ARGS {ARGS!r}", proc.stdout)

    def test_copy_of_the_shim_in_another_path_dir_is_skipped(self):
        other = Path(tempfile.mkdtemp(prefix="shim-copy-"))
        self.addCleanup(shutil.rmtree, other, True)
        shutil.copy2(SHIM, other / "uvicorn")
        proc = self.run_cmd(["uvicorn", *ARGS], path=f"{SHIM_DIR}:{other}:{self.real}:/usr/bin:/bin")
        self.assertEqual(proc.returncode, 7, proc.stdout + proc.stderr)
        self.assertIn(f"STUB_ARGS {ARGS!r}", proc.stdout)

    def test_only_copies_and_links_on_path_fail_loudly_without_looping(self):
        if any(os.access(f"{d}/uvicorn", os.X_OK) for d in ("/usr/bin", "/bin")):
            self.skipTest("a real uvicorn is on the system PATH")
        other = Path(tempfile.mkdtemp(prefix="shim-only-"))
        self.addCleanup(shutil.rmtree, other, True)
        shutil.copy2(SHIM, other / "uvicorn")
        proc = self.run_cmd(["uvicorn", *ARGS], path=f"{SHIM_DIR}:{other}:/usr/bin:/bin")
        self.assertEqual(proc.returncode, 127)
        self.assertIn("real_uvicorn_not_found", proc.stderr)

    def test_second_entry_is_refused_by_the_recursion_marker(self):
        proc = self.run_cmd(["uvicorn", *ARGS], {"WHOS_UVICORN_SHIM_ACTIVE": "1"})
        self.assertEqual(proc.returncode, 127)
        self.assertIn("UVICORN_SHIM_FAILED reason=recursion", proc.stderr)
        self.assertNotIn("STUB_ARGS", proc.stdout)

    def test_server_sees_the_marker_but_no_token(self):
        proc = self.run_cmd(["uvicorn", *ARGS], {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN})
        self.assertIn("STUB_ENV env=[] proc=[]", proc.stdout)


@unittest.skipUnless(shutil.which("bash") and os.path.exists("/proc/self/environ"), "bash and /proc are required")
class BootDeadlineTests(unittest.TestCase):
    """A hanging materialiser must not hold the boot for longer than the overall deadline."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="boot-deadline-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        scripts = self.root / "scripts"
        (scripts / "shim").mkdir(parents=True)
        shutil.copy2(SCRIPTS / "workspace_boot.sh", scripts / "workspace_boot.sh")
        shutil.copy2(SHIM, scripts / "shim" / "uvicorn")
        shutil.copy2(SCRIPTS / "materialise_workspaces.py", scripts / "materialise_workspaces.py")
        shutil.copy2(SCRIPTS / "pee_path_denylist_v1.json", scripts / "pee_path_denylist_v1.json")
        self.scripts = scripts
        self.real = self.root / "real"
        self.real.mkdir()
        (self.real / "python").symlink_to(sys.executable)
        stub = self.real / "uvicorn"
        stub.write_text(STUB.format(python=sys.executable), encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    def run_shim(self, extra=None):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("WHOS_")}
        env["PATH"] = f"{self.scripts / 'shim'}:{self.real}:{env.get('PATH', '')}"
        env.update(extra or {})
        started = time.monotonic()
        proc = subprocess.run(
            ["uvicorn", *ARGS], env=env, capture_output=True, text=True, timeout=60, cwd=str(self.root)
        )
        return proc, time.monotonic() - started

    def hang(self):
        # Stands in for a materialiser stuck on a slow fetch: leaves a staged tree, then never returns.
        (self.scripts / "materialise_workspaces.py").write_text(
            "import sys, time, pathlib\n"
            "if sys.argv[1:2] == ['--purge']:\n"
            "    import shutil\n"
            "    r = pathlib.Path(__file__).resolve().parent.parent / 'workspaces'\n"
            "    [shutil.rmtree(c) for c in r.iterdir()]\n"
            "    print('WORKSPACE_PURGED stub')\n"
            "    sys.exit(0)\n"
            "r = pathlib.Path(__file__).resolve().parent.parent / 'workspaces'\n"
            "(r / '.stage-partial').mkdir(parents=True)\n"
            "time.sleep(300)\n",
            encoding="utf-8",
        )

    def test_hanging_materialiser_is_cut_at_the_deadline_and_startup_continues(self):
        self.hang()
        proc, elapsed = self.run_shim(
            {"WHOS_WORKSPACE_BOOT_DEADLINE_SECONDS": "2", "WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN}
        )
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 7, out)
        self.assertLess(elapsed, 30)
        self.assertIn("WORKSPACE_REFUSED repo=* reason=deadline", proc.stdout)
        self.assertIn("WORKSPACE_PURGED", proc.stdout)
        self.assertEqual(list((self.root / "workspaces").iterdir()), [])
        self.assertIn(f"STUB_ARGS {ARGS!r}", proc.stdout)
        self.assertIn("STUB_ENV env=[] proc=[]", proc.stdout)
        self.assertNotIn(ORG_TOKEN, out)

    def test_invalid_deadline_falls_back_to_the_default_and_a_fast_run_is_untouched(self):
        proc, _ = self.run_shim({"WHOS_WORKSPACE_BOOT_DEADLINE_SECONDS": "abc"})
        self.assertEqual(proc.returncode, 7, proc.stdout + proc.stderr)
        self.assertIn("WORKSPACE_SKIPPED reason=unset", proc.stdout)
        self.assertNotIn("reason=deadline", proc.stdout)

    def test_real_purge_removes_stage_dirs_pin_dirs_and_files_even_when_read_only(self):
        root = self.root / "ws"
        (root / ".stage-x" / "tree").mkdir(parents=True)
        (root / "o__r__abcd1234" / "sub").mkdir(parents=True)
        (root / "o__r__abcd1234" / "sub" / "f.txt").write_text("x")
        (root / "o__r__abcd1234" / "sub").chmod(0o555)
        (root / "o__r__abcd1234").chmod(0o555)
        (root / "MANIFEST.txt").write_text("m")
        (root / "MANIFEST.txt").chmod(0o444)
        proc = subprocess.run(
            [sys.executable, "-I", str(self.scripts / "materialise_workspaces.py"), "--purge", str(root)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WORKSPACE_PURGED removed=3", proc.stdout)
        self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
