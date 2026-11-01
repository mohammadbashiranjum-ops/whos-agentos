"""Regression tests for scripts/materialise_workspaces.py.

Hermetic: GitHub is replaced by fake tarballs built in a temp directory and by a
fake HTTP opener; nothing touches the network, a database or the real /app. The
script is standard library only, so these run anywhere.

What is proven here (WHOS PEE phase 1 acceptance, design section 4):
  - default OFF: with WHOS_WORKSPACE_PINS unset the script does nothing;
  - a fixture tarball is extracted so that only allowed files exist, and every
    denied name (upper-case, mixed-case, Unicode, zero-width, fullwidth) is absent;
  - a manifest mismatch, a wrong commit, a bad pin, an unsafe member each refuse
    the repo and leave nothing behind;
  - symlink, hard link and device members are never created and path traversal or
    absolute members refuse the whole archive;
  - size and count bounds hold;
  - no token appears in captured stdout/stderr, in the tree, or in the manifest
    file, and the process environment is clean afterwards.

Run: python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

import contextlib
import email.message
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("materialise_workspaces.py")
_spec = importlib.util.spec_from_file_location("materialise_workspaces", SCRIPT)
mw = importlib.util.module_from_spec(_spec)
sys.modules["materialise_workspaces"] = mw
_spec.loader.exec_module(mw)

REPO = "wise-hands-company/whos-backend"
OTHER_REPO = "mohammadbashiranjum-ops/shaheen-army-build"
HEAD = "ae333227" + "0123456789abcdef0123456789abcdef"
assert len(HEAD) == 40
DIR = f"wise-hands-company__whos-backend__{HEAD[:8]}"
TOP = f"wise-hands-company-whos-backend-{HEAD[:7]}"
ORG_TOKEN = "ghp_" + "OrgTokenValue0123456789abcdefghij"
USER_TOKEN = "github_pat_" + "UserTokenValue0123456789ABCDEF"

ALLOWED_FILES = {
    "README.md": "# readme\n",
    "package.json": '{"name": "demo"}\n',
    "src/main.ts": "export const answer = 42;\n",
    "docs/guide.md": "Guide text.\n",
    "Docs/Mixed.MD": "Mixed case names that match no deny pattern stay.\n",
    "tests/test_a.py": "def test_a():\n    assert True\n",
    "prisma/migrations/001_init/migration.sql": "CREATE TABLE t (id INT);\n",
}

# Every one of these must be absent from the extracted tree: >= 40 names, with
# upper-case, mixed-case and Unicode variants (design 2.6, Phase 1 gate).
DENIED_NAMES = [
    ".env",
    ".ENV",
    ".env.production",
    ".Env.Local",
    ".gitignore",
    ".git/config",
    ".github/workflows/ci.yml",
    ".claude/settings.json",
    "KEY.PEM",
    "server.Pem",
    "private.key",
    "id_rsa",
    "ID_RSA.pub",
    "Id_Ed25519",
    "id_ecdsa",
    "cert.crt",
    "cert.CER",
    "a.der",
    "k.p8",
    "k.P12",
    "store.pfx",
    "ks.jks",
    "x.keystore",
    "x.gpg",
    "x.ppk",
    "db.kdbx",
    "secrets.json",
    "Secrets.JSON",
    "secrets/prod.txt",
    "credentials.yml",
    "CREDENTIALS.txt",
    "app.secret.txt",
    "state.tfstate",
    "state.TFSTATE.backup",
    "data.sqlite3",
    "DATA.DB",
    "dump.dump",
    "schema/dump.sql",
    "SCHEMA.SQL",
    "node_modules/x/index.js",
    "Node_Modules/y.js",
    "dist/a.js",
    "BUILD/a.txt",
    "coverage/lcov.info",
    "vendor/z.go",
    "pkg/__pycache__/m.cpython.txt",
    "package-lock.json",
    "Package-Lock.JSON",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "env.production",
    "prod.ENV",
    "logo.png",
    "img/Photo.JPG",
    "archive.ZIP",
    # Unicode and invisible-character variants
    "ｋｅｙ.ｐｅｍ",  # fullwidth "key.pem"
    "K​EY.pem",  # zero-width space inside the name
    ".env​",  # zero-width space after .env
    "sec‍retS.json",  # zero-width joiner: secrets.json
    "ＳＥＣＲＥＴＳ.json",  # fullwidth SECRETS.json
    "ﬁle.key",  # fi ligature: file.key
    "café.md",  # non-ASCII, conservative default
    "‮exe.pem",  # right-to-left override
    "name\twithtab.md",  # control character
    "back\\slash.md",  # backslash
    "has space.md",  # space is not printable ASCII (0x21..0x7E)
    "d/" + "x" * 200 + ".md",  # longer than 200 characters
    # Trailing dots and spaces are ignored by some filesystems: they must not evade the globs
    "key.pem.",
    "prod.env.",
    "id_rsa.",
    "secrets.json. ",
    ".env ",
    "cert.crt...",
    "keys/Server.KEY.",
    "dump/schema.sql.",
    "node_modules.",
    "...",
]

assert len(DENIED_NAMES) >= 40


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _add_file(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


def make_tarball(
    path: Path,
    files: dict,
    *,
    head: str | None = HEAD,
    top: str = TOP,
    extra=None,
) -> str:
    """Write a GitHub-shaped tarball: pax global header with the commit, one top directory."""

    pax = {"comment": head} if head else {}
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT, pax_headers=pax) as tar:
        top_info = tarfile.TarInfo(top)
        top_info.type = tarfile.DIRTYPE
        top_info.mode = 0o755
        tar.addfile(top_info)
        for name, content in files.items():
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            _add_file(tar, f"{top}/{name}", data)
        if extra:
            extra(tar, top)
    return str(path)


def expected_digest(files: dict) -> str:
    return mw.manifest_digest(
        [(n, mw.git_blob_sha1(c if isinstance(c, bytes) else c.encode())) for n, c in files.items()]
    )


def pins_json(digest: str, repo: str = REPO, head: str = HEAD, **extra) -> str:
    owner, name = repo.split("/")
    spec = {"head": head, "dir": f"{owner}__{name}__{head[:8]}", "manifest_sha256": digest}
    spec.update(extra)
    return json.dumps({"schema_version": 1, "repos": {repo: spec}})


class Fetcher:
    """Fake fetcher: copies a prepared tarball and records what it was asked for."""

    def __init__(self, tarball: str | None = None, raises: Exception | None = None):
        self.tarball = tarball
        self.raises = raises
        self.calls: list = []

    def __call__(self, repo, sha, token, dest, max_bytes):
        self.calls.append((repo, sha, token, max_bytes))
        if self.raises is not None:
            raise self.raises
        shutil.copyfile(self.tarball, dest)


def _chmod_tree_writable(root: Path) -> None:
    for current, dirs, files in os.walk(root):
        os.chmod(current, stat.S_IRWXU)
        for name in files:
            try:
                os.chmod(os.path.join(current, name), stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mw-test-"))
        self.addCleanup(self._cleanup)
        self.root = self.tmp / "workspaces"
        self.work = self.tmp / "work"
        self.work.mkdir()

    def _cleanup(self) -> None:
        _chmod_tree_writable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_job(self, pins, fetcher, environ=None, **kwargs):
        if environ is None:
            environ = {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN, "WHOS_WORKSPACE_READ_TOKEN_USER": USER_TOKEN}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mw.materialise(pins, environ, self.root, fetcher=fetcher, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def tree(self, base: Path | None = None) -> set:
        base = base or self.root / DIR
        found = set()
        for current, _dirs, files in os.walk(base):
            for name in files:
                found.add(os.path.relpath(os.path.join(current, name), base).replace(os.sep, "/"))
        return found


# --------------------------------------------------------------------------
# default OFF
# --------------------------------------------------------------------------
class DefaultOffTests(Base):
    def test_unset_pins_do_nothing(self):
        fetcher = Fetcher(raises=AssertionError("must not fetch"))
        environ = {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN}
        for pins in (None, "", "   \n"):
            code, out, err = self.run_job(pins, fetcher, environ)
            self.assertEqual(code, 0)
            self.assertEqual(out, "WORKSPACE_SKIPPED reason=unset\n")
            self.assertEqual(err, "")
        self.assertEqual(fetcher.calls, [])
        self.assertFalse(self.root.exists(), "default off must not create the workspaces directory")
        # Do nothing means nothing: the entrypoint, not this branch, removes tokens.
        self.assertIn("WHOS_WORKSPACE_READ_TOKEN_ORG", environ)

    def test_unset_pins_as_a_real_process_touches_nothing(self):
        env = {k: v for k, v in os.environ.items() if k != "WHOS_WORKSPACE_PINS"}
        env["WHOS_WORKSPACE_READ_TOKEN_ORG"] = ORG_TOKEN
        proc = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--root", str(self.root)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "WORKSPACE_SKIPPED reason=unset\n")
        self.assertEqual(proc.stderr, "")
        self.assertFalse(self.root.exists())

    def test_entrypoint_script_sets_the_default_root_beside_scripts(self):
        self.assertEqual(mw.DEFAULT_ROOT, SCRIPT.resolve().parent.parent / "workspaces")


# --------------------------------------------------------------------------
# extraction, deny-list
# --------------------------------------------------------------------------
class ExtractionTests(Base):
    def build(self, **kwargs):
        files = {**ALLOWED_FILES}
        for name in DENIED_NAMES:
            files[name] = "harmless text\n"
        tar = make_tarball(self.work / "a.tgz", files, **kwargs)
        return tar

    def test_only_allowed_files_survive_and_every_denied_name_is_absent(self):
        tar = self.build()
        digest = expected_digest(ALLOWED_FILES)
        code, out, err = self.run_job(pins_json(digest), Fetcher(tar), {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN})
        self.assertEqual(code, 0, out)
        self.assertIn(f"WORKSPACE_READY repo={REPO} head={HEAD[:8]} files={len(ALLOWED_FILES)} manifest=ok", out)
        self.assertEqual(self.tree(), set(ALLOWED_FILES))
        base = self.root / DIR
        for name in DENIED_NAMES:
            self.assertFalse(os.path.lexists(base / name), f"denied name present in tree: {name!r}")
        # A wide check that does not depend on spelling: no file name on disk folds to a denied pattern.
        rules = mw.DenyRules.load()
        for rel in self.tree():
            self.assertIsNone(mw.deny_reason(rel, rules), rel)

    def test_each_denied_name_alone_is_refused_by_the_rules(self):
        rules = mw.DenyRules.load()
        for name in DENIED_NAMES:
            with self.subTest(name=name):
                self.assertIsNotNone(mw.deny_reason(name, rules))
        for name in ALLOWED_FILES:
            with self.subTest(name=name):
                self.assertIsNone(mw.deny_reason(name, rules))

    def test_sql_is_denied_except_under_prisma_migrations(self):
        rules = mw.DenyRules.load()
        self.assertIsNone(mw.deny_reason("prisma/migrations/20260101_x/migration.sql", rules))
        self.assertIsNotNone(mw.deny_reason("prisma/other/dump.sql", rules))
        self.assertIsNotNone(mw.deny_reason("db/dump.sql", rules))
        self.assertIsNotNone(mw.deny_reason("PRISMA/MIGRATIONS/../x.sql", rules))

    def test_normalise_segment(self):
        self.assertEqual(mw.normalise_segment("KEY.PEM"), "key.pem")
        self.assertEqual(mw.normalise_segment("K​EY.pem"), "key.pem")
        self.assertEqual(mw.normalise_segment("ＫＥＹ.pem"), "key.pem")
        self.assertEqual(mw.normalise_segment("ﬁle"), "file")

    def test_withheld_content_is_listed_by_path_only(self):
        secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        files = {
            "README.md": "# readme\n",
            "src/config.ts": f"const t = '{secret}';\n",
            "src/pw.ts": "const pass" + "word = 'x1y2z3w4v5';\n",
            "assets/blob.txt": b"\xff\xfe\x00\x01",
            "src/huge.ts": "a" * (mw.Limits().max_file_bytes + 1),
        }
        tar = make_tarball(self.work / "w.tgz", files)
        result = mw.extract_tarball(Path(tar), HEAD, None, mw.DenyRules.load())
        self.assertEqual([p for p, _ in result.entries], ["README.md"])
        self.assertEqual(sorted(result.withheld), ["src/config.ts", "src/pw.ts"])
        self.assertEqual(result.skipped_binary, 1)
        self.assertEqual(result.skipped_oversize, 1)
        code, out, _ = self.run_job(pins_json(expected_digest({"README.md": "# readme\n"})), Fetcher(tar))
        self.assertIn("files=1", out)
        manifest = (self.root / "MANIFEST.txt").read_text()
        self.assertIn("WITHHELD_CREDENTIAL_SHAPED src/config.ts", manifest)
        self.assertNotIn(secret, manifest + out)
        self.assertFalse((self.root / DIR / "src").exists())

    def test_path_allow_limits_the_tree(self):
        tar = self.build()
        subset = {k: v for k, v in ALLOWED_FILES.items() if k.casefold().startswith(("src/", "docs/"))}
        code, out, _ = self.run_job(pins_json(expected_digest(subset), path_allow=["src/", "docs/"]), Fetcher(tar))
        self.assertIn("manifest=ok", out)
        self.assertEqual(self.tree(), set(subset))

    def test_tree_is_read_only_after_extraction(self):
        tar = self.build()
        self.run_job(pins_json(expected_digest(ALLOWED_FILES)), Fetcher(tar))
        mode = (self.root / DIR / "README.md").stat().st_mode
        self.assertEqual(mode & 0o222, 0)
        self.assertEqual((self.root / DIR).stat().st_mode & 0o222, 0)
        self.assertEqual((self.root / "MANIFEST.txt").stat().st_mode & 0o222, 0)

    def test_rerun_replaces_a_previous_tree(self):
        tar = self.build()
        digest = expected_digest(ALLOWED_FILES)
        self.run_job(pins_json(digest), Fetcher(tar))
        code, out, _ = self.run_job(pins_json(digest), Fetcher(tar))
        self.assertIn("manifest=ok", out)
        self.assertEqual(self.tree(), set(ALLOWED_FILES))

    def test_digest_only_helper_prints_the_pin_digest(self):
        tar = self.build()
        proc = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--digest-only", tar, "--head", HEAD],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"MANIFEST_SHA256 {expected_digest(ALLOWED_FILES)} files={len(ALLOWED_FILES)}", proc.stdout)
        self.assertFalse(self.root.exists())


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------
class RefusalTests(Base):
    def tar(self, files=None, **kwargs):
        return make_tarball(self.work / "r.tgz", ALLOWED_FILES if files is None else files, **kwargs)

    def assert_refused(self, out, reason, repo=REPO):
        self.assertIn(f"WORKSPACE_REFUSED repo={repo} reason={reason}", out)
        self.assertNotIn("WORKSPACE_READY", out)
        self.assertFalse((self.root / DIR).exists(), "a refused repo must not be exposed")
        leftovers = [p.name for p in self.root.iterdir()] if self.root.exists() else []
        self.assertEqual(leftovers, [], f"nothing may be left behind: {leftovers}")

    def test_manifest_mismatch_is_refused_and_deleted(self):
        # An earlier boot left a tree at the same path: a refusal removes it too.
        (self.root / DIR).mkdir(parents=True)
        (self.root / DIR / "stale.txt").write_text("stale")
        code, out, _ = self.run_job(pins_json("0" * 64), Fetcher(self.tar()))
        self.assertEqual(code, 0)
        self.assert_refused(out, "manifest_mismatch")

    def test_one_changed_byte_changes_the_digest(self):
        changed = {**ALLOWED_FILES, "src/main.ts": "export const answer = 43;\n"}
        code, out, _ = self.run_job(pins_json(expected_digest(ALLOWED_FILES)), Fetcher(self.tar(changed)))
        self.assert_refused(out, "manifest_mismatch")

    def test_wrong_commit_in_pax_header_is_refused(self):
        other = "b" * 40
        code, out, _ = self.run_job(pins_json(expected_digest(ALLOWED_FILES)), Fetcher(self.tar(head=other)))
        self.assert_refused(out, "commit_mismatch")

    def test_missing_pax_commit_is_refused(self):
        code, out, _ = self.run_job(pins_json(expected_digest(ALLOWED_FILES)), Fetcher(self.tar(head=None)))
        self.assert_refused(out, "commit_mismatch")

    def test_top_directory_name_alone_is_not_identity(self):
        # The directory name carries 7 hex characters of the commit; identity rests on the pax header.
        code, out, _ = self.run_job(
            pins_json(expected_digest(ALLOWED_FILES)), Fetcher(self.tar(head="c" * 40, top=TOP))
        )
        self.assert_refused(out, "commit_mismatch")

    def test_bad_pins_are_refused_before_any_fetch(self):
        digest = expected_digest(ALLOWED_FILES)
        cases = {
            "pins_invalid": ["not json", "[]", "{}", json.dumps({"repos": {}})],
            "repo_not_allowed": [
                pins_json(digest, repo="evil/other"),
                pins_json(digest, repo="mohammadbashiranjum-ops/whos-agentos"),
            ],
            "head_invalid": [
                pins_json(digest, head="main"),
                pins_json(digest, head=HEAD[:7]),
                pins_json(digest, head=HEAD.upper()),
                pins_json(digest, head="g" * 40),
            ],
            "manifest_pin_missing": [pins_json("short"), pins_json("")],
        }
        for reason, variants in cases.items():
            for pins in variants:
                with self.subTest(reason=reason, pins=pins[:60]):
                    fetcher = Fetcher(self.tar())
                    code, out, _ = self.run_job(pins, fetcher)
                    self.assertEqual(code, 0)
                    self.assertIn(f"reason={reason}", out)
                    self.assertEqual(fetcher.calls, [], "a bad pin must not cause a request")
        spec = json.loads(pins_json(digest))
        spec["repos"][REPO]["dir"] = "wise-hands-company__whos-backend__deadbeef"
        fetcher = Fetcher(self.tar())
        _, out, _ = self.run_job(json.dumps(spec), fetcher)
        self.assertIn("reason=dir_invalid", out)
        self.assertEqual(fetcher.calls, [])

    def test_private_repo_without_token_is_unavailable_public_is_anonymous(self):
        digest = expected_digest(ALLOWED_FILES)
        fetcher = Fetcher(self.tar())
        _, out, _ = self.run_job(pins_json(digest), fetcher, {})
        self.assertIn("reason=token_missing", out)
        self.assertEqual(fetcher.calls, [])
        public = Fetcher(
            make_tarball(self.work / "p.tgz", ALLOWED_FILES, top="mohammadbashiranjum-ops-shaheen-army-build-ae33322")
        )
        _, out, _ = self.run_job(pins_json(digest, repo=OTHER_REPO, private=False), public, {})
        self.assertIn(f"WORKSPACE_READY repo={OTHER_REPO}", out)
        self.assertEqual(public.calls[0][2], None, "no token configured: the request is anonymous")

    def test_token_is_selected_by_owner(self):
        digest = expected_digest(ALLOWED_FILES)
        environ = {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN, "WHOS_WORKSPACE_READ_TOKEN_USER": USER_TOKEN}
        org = Fetcher(self.tar())
        self.run_job(pins_json(digest), org, dict(environ))
        self.assertEqual(org.calls[0][2], ORG_TOKEN)
        user = Fetcher(make_tarball(self.work / "u.tgz", ALLOWED_FILES, top="x-y-ae33322"))
        self.run_job(pins_json(digest, repo=OTHER_REPO), user, dict(environ))
        self.assertEqual(user.calls[0][2], USER_TOKEN)
        # Full 40-hex commit, never a branch.
        self.assertEqual(org.calls[0][1], HEAD)

    def test_invalid_token_shape_is_refused_without_printing_it(self):
        bad = "ghp_x\nInjected: header"
        fetcher = Fetcher(self.tar())
        _, out, err = self.run_job(
            pins_json(expected_digest(ALLOWED_FILES)), fetcher, {"WHOS_WORKSPACE_READ_TOKEN_ORG": bad}
        )
        self.assertIn("reason=token_invalid", out)
        self.assertNotIn("Injected", out + err)
        self.assertEqual(fetcher.calls, [])

    def test_fetch_failures_are_reason_codes(self):
        for raised, reason in (
            (mw.MaterialiseError("http_404"), "http_404"),
            (mw.MaterialiseError("network_error"), "network_error"),
        ):
            _, out, _ = self.run_job(
                pins_json(expected_digest(ALLOWED_FILES)),
                Fetcher(raises=raised),
                {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN},
            )
            self.assertIn(f"reason={reason}", out)

    def test_corrupt_archive_is_refused(self):
        bad = self.work / "bad.tgz"
        bad.write_bytes(b"this is not a gzip file")
        _, out, _ = self.run_job(pins_json(expected_digest(ALLOWED_FILES)), Fetcher(str(bad)))
        self.assert_refused(out, "bad_archive")

    def test_empty_tree_is_refused(self):
        tar = self.tar({".env": "x\n"})
        _, out, _ = self.run_job(pins_json(expected_digest({})), Fetcher(tar))
        self.assert_refused(out, "empty_tree")


# --------------------------------------------------------------------------
# hostile tar members
# --------------------------------------------------------------------------
class HostileMemberTests(Base):
    def refuse_case(self, name, extra, reason="unsafe_member"):
        sentinel = self.tmp / "evil.txt"
        tar = make_tarball(self.work / f"{name}.tgz", ALLOWED_FILES, extra=extra)
        _, out, _ = self.run_job(pins_json(expected_digest(ALLOWED_FILES)), Fetcher(tar))
        self.assertIn(f"WORKSPACE_REFUSED repo={REPO} reason={reason}", out, name)
        self.assertNotIn("WORKSPACE_READY", out)
        self.assertFalse(sentinel.exists())
        self.assertFalse((self.root / DIR).exists())
        self.assertEqual([p.name for p in self.root.iterdir()], [])

    def test_path_traversal_member_refuses_the_archive(self):
        self.refuse_case("trav1", lambda t, top: _add_file(t, f"{top}/../../evil.txt", b"x"))
        self.refuse_case("trav2", lambda t, top: _add_file(t, f"{top}/a/../../../evil.txt", b"x"))
        self.refuse_case("trav3", lambda t, top: _add_file(t, "../evil.txt", b"x"))

    def test_absolute_path_member_refuses_the_archive(self):
        evil = str(self.tmp / "evil.txt")
        self.refuse_case("abs1", lambda t, top: _add_file(t, evil, b"x"))
        self.refuse_case("abs2", lambda t, top: _add_file(t, "/etc/cron.d/evil", b"x"))

    def test_member_outside_the_single_top_directory_refuses_the_archive(self):
        self.refuse_case("top2", lambda t, top: _add_file(t, "other-top/file.txt", b"x"), "layout_unexpected")
        self.refuse_case("top3", lambda t, top: _add_file(t, "loose.txt", b"x"), "layout_unexpected")

    def test_dot_and_empty_segments_refuse_the_archive(self):
        self.refuse_case("dot", lambda t, top: _add_file(t, f"{top}/./x.txt", b"x"))

    def test_symlink_hardlink_and_special_members_are_never_created(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside secret")

        def extra(tar, top):
            for name, target in (("link_abs", str(outside)), ("link_rel", "../../outside"), ("link_file", "README.md")):
                info = tarfile.TarInfo(f"{top}/{name}")
                info.type = tarfile.SYMTYPE
                info.linkname = target
                tar.addfile(info)
            hard = tarfile.TarInfo(f"{top}/hard")
            hard.type = tarfile.LNKTYPE
            hard.linkname = f"{top}/README.md"
            tar.addfile(hard)
            for name, kind in (("dev", tarfile.CHRTYPE), ("blk", tarfile.BLKTYPE), ("fifo", tarfile.FIFOTYPE)):
                info = tarfile.TarInfo(f"{top}/{name}")
                info.type = kind
                tar.addfile(info)
            # A file written "through" the symlink name lands in a real directory of the tree,
            # never in the symlink's target, because no link is ever created.
            _add_file(tar, f"{top}/link_abs/pwn.txt", b"pwn")

        tar = make_tarball(self.work / "links.tgz", ALLOWED_FILES, extra=extra)
        files = {**ALLOWED_FILES, "link_abs/pwn.txt": "pwn"}
        result = mw.extract_tarball(Path(tar), HEAD, None, mw.DenyRules.load())
        self.assertEqual(result.links_refused, 7)
        _, out, _ = self.run_job(pins_json(expected_digest(files)), Fetcher(tar))
        self.assertIn("manifest=ok", out)
        base = self.root / DIR
        for name in ("link_rel", "link_file", "hard", "dev", "blk", "fifo"):
            self.assertFalse(os.path.lexists(base / name), name)
        self.assertFalse((base / "link_abs").is_symlink())
        self.assertEqual(list(outside.iterdir()), [outside / "secret.txt"], "nothing may reach the link target")
        self.assertEqual(sum(1 for p in base.rglob("*") if p.is_symlink()), 0)

    def test_file_then_directory_of_the_same_name_is_a_layout_conflict(self):
        self.refuse_case(
            "conflict",
            lambda t, top: (_add_file(t, f"{top}/clash", b"a"), _add_file(t, f"{top}/clash/inner.txt", b"b")),
            "layout_conflict",
        )

    def test_duplicate_member_refuses_the_archive(self):
        self.refuse_case("dup", lambda t, top: _add_file(t, f"{top}/README.md", b"second"), "duplicate_member")


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------
class BoundsTests(Base):
    def test_too_many_files(self):
        files = {f"f{i}.txt": f"line {i}\n" for i in range(12)}
        tar = make_tarball(self.work / "n.tgz", files)
        _, out, _ = self.run_job(pins_json(expected_digest(files)), Fetcher(tar), limits=mw.Limits(max_files=10))
        self.assertIn("reason=too_many_files", out)
        self.assertFalse((self.root / DIR).exists())
        files_ok = dict(list(files.items())[:10])
        tar = make_tarball(self.work / "n2.tgz", files_ok)
        _, out, _ = self.run_job(pins_json(expected_digest(files_ok)), Fetcher(tar), limits=mw.Limits(max_files=10))
        self.assertIn("files=10 manifest=ok", out)

    def test_too_many_members_counts_denied_ones_too(self):
        files = {f".hidden{i}": "x\n" for i in range(30)}
        files["README.md"] = "ok\n"
        tar = make_tarball(self.work / "m.tgz", files)
        _, out, _ = self.run_job(
            pins_json(expected_digest({"README.md": "ok\n"})), Fetcher(tar), limits=mw.Limits(max_members=20)
        )
        self.assertIn("reason=too_many_members", out)

    def test_total_extracted_bytes_bound(self):
        files = {f"f{i}.txt": ("word " * 200) + f"{i}\n" for i in range(6)}
        tar = make_tarball(self.work / "b.tgz", files)
        _, out, _ = self.run_job(
            pins_json(expected_digest(files)), Fetcher(tar), limits=mw.Limits(max_total_bytes=3000)
        )
        self.assertIn("reason=too_large", out)
        self.assertFalse((self.root / DIR).exists())

    def test_declared_unpacked_bytes_bound_counts_skipped_files(self):
        files = {"README.md": "ok\n", "big.bin.txt": "z" * 4000}
        tar = make_tarball(self.work / "u.tgz", files)
        _, out, _ = self.run_job(
            pins_json(expected_digest({"README.md": "ok\n"})), Fetcher(tar), limits=mw.Limits(max_unpacked_bytes=1000)
        )
        self.assertIn("reason=too_large", out)

    def test_single_file_over_the_cap_is_skipped_not_extracted(self):
        files = {"README.md": "ok\n", "src/big.ts": "a" * 600}
        tar = make_tarball(self.work / "o.tgz", files)
        result = mw.extract_tarball(Path(tar), HEAD, None, mw.DenyRules.load(), mw.Limits(max_file_bytes=500))
        self.assertEqual([p for p, _ in result.entries], ["README.md"])
        self.assertEqual(result.skipped_oversize, 1)

    def test_default_bounds_are_the_design_numbers(self):
        limits = mw.Limits()
        self.assertEqual(limits.max_file_bytes, 512 * 1024)
        self.assertGreater(limits.max_files, 0)
        self.assertGreater(limits.max_total_bytes, limits.max_file_bytes)


# --------------------------------------------------------------------------
# tokens: never printed, always removed
# --------------------------------------------------------------------------
class TokenTests(Base):
    def all_text_under_root(self) -> str:
        chunks = []
        if self.root.exists():
            for current, _dirs, files in os.walk(self.root):
                for name in files:
                    chunks.append(os.path.join(current, name))
                    chunks.append((Path(current) / name).read_text(errors="replace"))
        return "\n".join(chunks)

    def test_success_prints_no_token_and_cleans_the_environment(self):
        files = {**ALLOWED_FILES}
        tar = make_tarball(self.work / "t.tgz", files)
        environ = {
            "WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN,
            "WHOS_WORKSPACE_READ_TOKEN_USER": USER_TOKEN,
            "UNRELATED": "keep",
        }
        _, out, err = self.run_job(pins_json(expected_digest(files)), Fetcher(tar), environ)
        for token in (ORG_TOKEN, USER_TOKEN):
            self.assertNotIn(token, out + err + self.all_text_under_root())
        self.assertIn("TOKEN_SCRUBBED removed=2", out)
        self.assertEqual(environ, {"UNRELATED": "keep"})

    def test_failures_print_no_token_even_when_the_exception_holds_one(self):
        for raised in (
            RuntimeError(f"boom {ORG_TOKEN} https://x.invalid/?token={ORG_TOKEN}"),
            mw.MaterialiseError("http_401"),
            urllib.error.URLError(f"https://user:{ORG_TOKEN}@api.github.com/"),
            OSError(ORG_TOKEN),
        ):
            environ = {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN}
            _, out, err = self.run_job(pins_json("1" * 64), Fetcher(raises=raised), environ)
            self.assertNotIn(ORG_TOKEN, out + err)
            self.assertIn("WORKSPACE_REFUSED", out)
            self.assertIn("TOKEN_SCRUBBED removed=1", out)
            self.assertEqual(environ, {})

    def test_the_logger_redacts_a_secret_even_if_a_line_carries_it(self):
        log = mw.Log()
        log.add_secret(ORG_TOKEN)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            log(f"anything {ORG_TOKEN} anything")
        self.assertNotIn(ORG_TOKEN, out.getvalue())
        self.assertIn("[REDACTED]", out.getvalue())

    def test_pins_error_still_scrubs_tokens(self):
        environ = {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN, "whos_workspace_read_token_user": USER_TOKEN}
        _, out, _ = self.run_job("not json", Fetcher(raises=AssertionError("no")), environ)
        self.assertIn("reason=pins_invalid", out)
        self.assertEqual(environ, {})

    def test_real_process_environment_is_clean_after_a_run(self):
        files = {**ALLOWED_FILES}
        tar = make_tarball(self.work / "e.tgz", files)
        env = {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN, "WHOS_WORKSPACE_READ_TOKEN_USER": USER_TOKEN}
        with mock.patch.dict(os.environ, env, clear=False):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                mw.materialise(pins_json(expected_digest(files)), os.environ, self.root, fetcher=Fetcher(tar))
            self.assertEqual([k for k in os.environ if k.startswith("WHOS_WORKSPACE_READ_TOKEN_")], [])
            self.assertNotIn(ORG_TOKEN, out.getvalue())


# --------------------------------------------------------------------------
# the real fetcher against a fake opener
# --------------------------------------------------------------------------
class _Response:
    def __init__(self, payload: bytes):
        self._payload = payload
        self._pos = 0
        self.closed = False

    def read(self, n=-1):
        chunk = self._payload[self._pos : self._pos + (n if n > 0 else len(self._payload))]
        self._pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True


def _redirect(url: str, location: str | None):
    headers = email.message.Message()
    if location is not None:
        headers["Location"] = location
    return urllib.error.HTTPError(url, 302, "Found", headers, io.BytesIO(b""))


class FakeOpener:
    def __init__(self, script):
        self.script = list(script)
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append((request.full_url, dict(request.header_items())))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class GitHubFetcherTests(Base):
    CODELOAD = "https://codeload.github.com/wise-hands-company/whos-backend/legacy.tar.gz/" + HEAD
    QUERY_TOKEN = "AQUERYTOKEN0123456789abcdef"

    def fetch(self, script, token=ORG_TOKEN, max_bytes=10_000_000):
        log = mw.Log()
        opener = FakeOpener(script)
        fetcher = mw.GitHubFetcher(opener=opener, log=log)
        out = io.StringIO()
        dest = self.work / "dl.tgz"
        error = None
        with contextlib.redirect_stdout(out):
            try:
                fetcher(REPO, HEAD, token, dest, max_bytes)
            except mw.MaterialiseError as err:
                error = err.reason
        return opener, dest, error, log

    def test_authorization_goes_to_api_only_and_is_stripped_from_the_redirect(self):
        api = f"https://api.github.com/repos/{REPO}/tarball/{HEAD}"
        location = f"{self.CODELOAD}?token={self.QUERY_TOKEN}"
        opener, dest, error, _ = self.fetch([_redirect(api, location), _Response(b"payload")])
        self.assertIsNone(error)
        self.assertEqual(dest.read_bytes(), b"payload")
        first_url, first_headers = opener.requests[0]
        second_url, second_headers = opener.requests[1]
        self.assertEqual(first_url, api)
        self.assertEqual(first_headers.get("Authorization"), "Bearer " + ORG_TOKEN)
        self.assertEqual(second_url, location)
        self.assertNotIn("Authorization", {k.title() for k in second_headers})

    def test_no_token_means_no_authorization_header(self):
        opener, _, error, _ = self.fetch([_Response(b"direct")], token=None)
        self.assertIsNone(error)
        self.assertNotIn("Authorization", {k.title() for k in opener.requests[0][1]})

    def test_redirect_to_another_host_scheme_or_userinfo_is_refused(self):
        api = f"https://api.github.com/repos/{REPO}/tarball/{HEAD}"
        for location in (
            "https://evil.example/x.tar.gz",
            "http://codeload.github.com/x",
            "https://codeload.github.com.evil.example/x",
            "https://user:pw@codeload.github.com/x",
            "https://api.github.com/other",
            None,
        ):
            with self.subTest(location=location):
                opener, dest, error, _ = self.fetch([_redirect(api, location), _Response(b"never")])
                self.assertEqual(error, "redirect_refused")
                self.assertEqual(len(opener.requests), 1, "the redirect target must not be requested")
                self.assertFalse(dest.exists())

    def test_http_errors_are_reason_codes(self):
        api = f"https://api.github.com/repos/{REPO}/tarball/{HEAD}"
        err404 = urllib.error.HTTPError(api, 404, "Not Found", email.message.Message(), io.BytesIO(b""))
        _, _, error, _ = self.fetch([err404])
        self.assertEqual(error, "http_404")
        _, _, error, _ = self.fetch([urllib.error.URLError(f"https://x:{ORG_TOKEN}@host")])
        self.assertEqual(error, "network_error")

    def test_oversize_download_is_cut_off(self):
        _, _, error, _ = self.fetch([_Response(b"a" * 5000)], max_bytes=1000)
        self.assertEqual(error, "tarball_too_large")

    def test_redirect_url_and_its_token_never_reach_the_log(self):
        api = f"https://api.github.com/repos/{REPO}/tarball/{HEAD}"
        location = f"{self.CODELOAD}?token={self.QUERY_TOKEN}"
        tar = make_tarball(self.work / "r.tgz", ALLOWED_FILES)
        opener = FakeOpener([_redirect(api, location), _Response(Path(tar).read_bytes())])
        log = mw.Log()
        fetcher = mw.GitHubFetcher(opener=opener, log=log)
        out, err = io.StringIO(), io.StringIO()
        environ = {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            mw.materialise(pins_json(expected_digest(ALLOWED_FILES)), environ, self.root, fetcher=fetcher, log=log)
            # A failing second hop that would print the URL in a naive implementation.
            opener2 = FakeOpener([_redirect(api, location), urllib.error.URLError(location)])
            mw.materialise(
                pins_json(expected_digest(ALLOWED_FILES)),
                {"WHOS_WORKSPACE_READ_TOKEN_ORG": ORG_TOKEN},
                self.root,
                fetcher=mw.GitHubFetcher(opener=opener2, log=log),
                log=log,
            )
        text = out.getvalue() + err.getvalue()
        self.assertIn("WORKSPACE_READY", text)
        self.assertIn("reason=network_error", text)
        for secret in (ORG_TOKEN, self.QUERY_TOKEN, "codeload.github.com", "Bearer", "Authorization"):
            self.assertNotIn(secret, text)


# --------------------------------------------------------------------------
# credential content rule (stdlib copy of the producer's detector)
# --------------------------------------------------------------------------
class CredentialRuleTests(unittest.TestCase):
    def test_shapes_the_producer_withholds_are_withheld(self):
        samples = [
            "token = ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
            "github_pat_" + "11ABCDEFG0abcdefghijklmnop_qrstuv",
            "AKIA" + "ABCDEFGHIJKLMNOP",
            "-----BEGIN RSA " + "PRIVATE KEY-----",
            "postgres://user:" + "hunter2pass@db.internal/app",
            "Authorization: Bear" + "er abcdefghijklmnopqrstuvwxyz0123456789",
            "pass" + "word = 'x1y2z3w4v5'",
            "eyJhbGciOiJI" + ".eyJzdWIiOiIx" + ".SflKxwRJSMeKKF2Q",
        ]
        for sample in samples:
            with self.subTest(sample=sample[:24]):
                self.assertTrue(mw.carries_credential(sample + "\n"))

    def test_ordinary_source_is_not_withheld(self):
        for sample in (
            "export const answer = 42;\n",
            "const password = process.env.DB_PASSWORD;\n",
            "see docs/guide.md for the token flow\n",
            "commit " + "a" * 40 + "\n",
        ):
            with self.subTest(sample=sample[:24]):
                self.assertFalse(mw.carries_credential(sample))

    def test_only_the_first_twenty_thousand_characters_are_scanned(self):
        late = "x" * 20_001 + "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
        self.assertFalse(mw.carries_credential(late))


if __name__ == "__main__":
    unittest.main()
