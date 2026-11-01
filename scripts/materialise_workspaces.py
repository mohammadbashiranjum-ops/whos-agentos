#!/usr/bin/env python3
"""Materialise pinned, read-only copies of other repositories under /app/workspaces.

WHOS PEE, phase 1 (design: whos-backend docs/WHOS_PEE_AGENTOS_MULTIREPO_DESIGN_V1.md,
sections 2 and 2.5, Appendix A). Standard library only: it runs from
scripts/entrypoint.sh before the application starts and must not import the app.

DEFAULT OFF. With WHOS_WORKSPACE_PINS unset or empty this script prints one line
(WORKSPACE_SKIPPED reason=unset) and does nothing else: no network, no file, no
directory.

When WHOS_WORKSPACE_PINS is set it holds JSON (names and digests only, no secret):

    {"schema_version": 1,
     "repos": {"wise-hands-company/whos-backend": {
         "head": "<40 lowercase hex>",
         "dir": "wise-hands-company__whos-backend__<first 8 of head>",
         "manifest_sha256": "<64 hex>",
         "path_allow": ["src/", "docs/"],      (optional; absent or empty = whole tree)
         "private": true,                      (optional; default true)
         "token": "ORG"                        (optional; ORG or USER; default by owner)
     }}}

For each repo it downloads the GitHub tarball of the FULL 40-hex commit (never a
branch), checks the commit recorded in the archive's pax global header, extracts
only allowed regular files (deny-list in scripts/pee_path_denylist_v1.json, plus a
credential-shape content rule), recomputes the manifest digest and compares it with
the pin. A mismatch or any other failure removes that repo and prints
WORKSPACE_REFUSED. The process always exits 0 so a bad pin or token cannot put the
service in a restart loop (railway.json restarts on failure); the app-start
self-check and the pin check, not the exit status, decide what is served.

Secrets: read tokens come from WHOS_WORKSPACE_READ_TOKEN_ORG / _USER. They are
sent in an Authorization header to api.github.com only, never forwarded to the
redirect target, never logged, and removed from this process's environment at the
end (TOKEN_SCRUBBED). No URL, header, token or file content is ever printed; the
log carries repository names, short commit ids, counts and reason codes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DENYLIST_PATH = SCRIPT_DIR / "pee_path_denylist_v1.json"
DEFAULT_ROOT = SCRIPT_DIR.parent / "workspaces"

PINS_ENV = "WHOS_WORKSPACE_PINS"
TOKEN_PREFIX = "WHOS_WORKSPACE_READ_TOKEN_"
TOKEN_SELECTORS = ("ORG", "USER")
ORG_OWNER = "wise-hands-company"

# Exact allow-list (design 2.3). whos-agentos itself is already /app and is not materialised.
ALLOWED_REPOS = frozenset(
    {
        "wise-hands-company/whos-backend",
        "wise-hands-company/whos-frontend",
        "mohammadbashiranjum-ops/shaheen-army-build",
    }
)

API_HOST = "api.github.com"
CODELOAD_HOST = "codeload.github.com"
FETCH_TIMEOUT_SECONDS = 60

# Exception tuples live in names: `except A, B:` without parentheses is Python 3.14 syntax
# (PEP 758), and this module's tests also run on 3.13.
_NET_ERRORS = (urllib.error.URLError, OSError, ValueError)
_STREAM_ERRORS = (OSError, ValueError)
_ARCHIVE_ERRORS = (tarfile.TarError, EOFError, OSError, zlib.error)
_CONFLICT_ERRORS = (FileExistsError, NotADirectoryError)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,255}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")


MAX_PATH_LENGTH = 200  # same bound as the producer-side deny-list (tools/whos_pee_path_denylist.py)


@dataclass(frozen=True)
class Limits:
    """Bounds that keep a hostile or oversized archive from filling the disk or the clock."""

    max_tarball_bytes: int = 100 * 1024 * 1024  # compressed download
    max_members: int = 100_000  # tar entries of any kind
    max_unpacked_bytes: int = 512 * 1024 * 1024  # sum of declared member sizes
    max_files: int = 20_000  # extracted files per repo
    max_total_bytes: int = 100 * 1024 * 1024  # extracted bytes per repo
    max_file_bytes: int = 512 * 1024  # one file


class MaterialiseError(Exception):
    """A repo is refused. ``reason`` is a short code; the message is never printed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------
# Credential-shape content rule. A stdlib copy of CREDENTIAL_SHAPES and
# carries_credential() from whos-backend tools/whos_agentos_answer_readback.py
# (the script cannot import that repository). Parity is checked from the
# whos-backend side.
# --------------------------------------------------------------------------
CREDENTIAL_SHAPES = re.compile(
    r"agno_pat_[A-Za-z0-9_-]{8,}"
    r"|whos_sys_[A-Za-z0-9_-]{8,}"
    r"|whos-oh\d+-[A-Za-z0-9]{6,}"
    r"|sb_secret_[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk[-_](?:live|test|proj|ant)?[-_]?[A-Za-z0-9_-]{20,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{30,}"
    r"|xox[abprse]-[A-Za-z0-9-]{10,}"
    r"|npm_[A-Za-z0-9]{30,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|[A-Za-z][A-Za-z0-9+.-]{0,20}://[^\s/@:{}<>$%]+:[^\s/@{}<>$%]+@"
    r"|(?i:bearer)\s*[A-Za-z0-9._~+/-]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
_ASSIGNED_SECRET = re.compile(
    r"(?i)[\w.-]{0,40}(?:pass|pwd|secret|token|key|cred|auth)[\w.-]{0,40}[\"']?\s{0,4}[:=]\s{0,4}[\"']?([^\s\"'<>(),;{}]{8,})"
)
_RUN = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HEX = re.compile(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def _compact(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if not ch.isspace() and unicodedata.category(ch) not in ("Cf", "Cc"))


def _entropy(text: str) -> float:
    import math

    counts = {ch: text.count(ch) for ch in set(text)}
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def _looks_like_a_secret_value(value: str) -> bool:
    return any(ch.isdigit() for ch in value) and any(ch.isalpha() for ch in value) and "(" not in value


def carries_credential(content: str, scan_chars: int = 20000) -> bool:
    """Whether the first ``scan_chars`` of ``content`` hold anything credential-shaped."""

    content = content[:scan_chars]
    if CREDENTIAL_SHAPES.search(content):
        return True
    if any(any(ch.isdigit() for ch in m.group(0)) for m in CREDENTIAL_SHAPES.finditer(_compact(content))):
        return True
    if any(_looks_like_a_secret_value(m.group(1)) for m in _ASSIGNED_SECRET.finditer(content)):
        return True
    for run in _RUN.findall(content):
        if _UUID.fullmatch(run) or _HEX.fullmatch(run):
            continue
        if any(ch.isdigit() for ch in run) and any(ch.isalpha() for ch in run) and _entropy(run) >= 3.5:
            return True
    return False


# --------------------------------------------------------------------------
# Path deny-list
# --------------------------------------------------------------------------
def normalise_segment(segment: str) -> str:
    """Strip Cc/Cf, NFKC, casefold, NFKC, strip Cc/Cf (the order the shared deny-list file states)."""

    def strip(value: str) -> str:
        return "".join(ch for ch in value if unicodedata.category(ch) not in ("Cc", "Cf"))

    text = unicodedata.normalize("NFKC", strip(segment)).casefold()
    return strip(unicodedata.normalize("NFKC", text))


@dataclass
class DenyRules:
    segment_regex: re.Pattern[str]
    conditional: list  # list of (compiled regex, tuple of allowed prefixes)
    credential_scan_chars: int
    max_file_bytes: int

    @classmethod
    def load(cls, path: Path = DENYLIST_PATH) -> DenyRules:
        data = json.loads(path.read_text(encoding="utf-8"))
        patterns = [str(p).casefold() for p in data["segment_patterns"]]
        regex = re.compile("|".join(fnmatch.translate(p) for p in patterns))
        conditional = [
            (
                re.compile(fnmatch.translate(str(c["pattern"]).casefold())),
                tuple(str(p).casefold() for p in c.get("except_under", [])),
            )
            for c in data.get("conditional_patterns", [])
        ]
        limits = data.get("limits", {})
        return cls(
            segment_regex=regex,
            conditional=conditional,
            credential_scan_chars=int(limits.get("credential_scan_chars", 20000)),
            max_file_bytes=int(limits.get("max_file_bytes", 512 * 1024)),
        )


def deny_reason(rel_path: str, rules: DenyRules) -> str | None:
    """Return a short reason code when ``rel_path`` (repo-relative, '/'-separated) must not be extracted."""

    if len(rel_path) > MAX_PATH_LENGTH:
        return "too_long"
    if "\\" in rel_path:
        return "backslash"
    for ch in rel_path:
        code = ord(ch)
        if code < 32 or code == 127 or unicodedata.category(ch) in ("Cc", "Cf"):
            return "control_char"
    segments = rel_path.split("/")
    if any(s in ("", ".", "..") for s in segments):
        return "bad_segment"
    normalised = [normalise_segment(s) for s in segments]
    # Trailing dots and spaces are ignored by some filesystems, so ``key.pem.`` is also tested as ``key.pem``:
    # a segment is denied when either its normalised form or its stripped form matches.
    stripped = [seg.rstrip(". ") for seg in normalised]
    if any(not seg for seg in stripped):
        return "bad_segment"
    for seg in set(normalised) | set(stripped):
        if rules.segment_regex.match(seg):
            return "deny_pattern"
    folded_path = "/".join(normalised)
    for regex, allowed_prefixes in rules.conditional:
        if (regex.match(normalised[-1]) or regex.match(stripped[-1])) and not any(
            folded_path.startswith(p) for p in allowed_prefixes
        ):
            return "deny_pattern"
    # Conservative default (design 2.6): a name that is not printable ASCII after normalisation is dropped.
    # A space is not printable ASCII here either (0x21..0x7E only), as in the producer-side rule.
    if any(not 0x20 < ord(ch) < 0x7F for ch in folded_path.replace("/", "")):
        return "non_ascii"
    return None


def path_allowed_by_prefix(rel_path: str, path_allow: list) -> bool:
    if not path_allow:
        return True
    folded = "/".join(normalise_segment(s) for s in rel_path.split("/"))
    return any(
        folded.startswith(normalise_segment(p.rstrip("/")) + "/") or folded == normalise_segment(p) for p in path_allow
    )


# --------------------------------------------------------------------------
# Manifest digest: sha256 over sorted "path NUL git-blob-sha1 LF" lines of the extracted files.
# --------------------------------------------------------------------------
def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 (git object id, not security)


def manifest_digest(entries: list) -> str:
    digest = hashlib.sha256()
    for path, blob in sorted(entries, key=lambda e: e[0].encode("utf-8")):
        digest.update(path.encode("utf-8") + b"\0" + blob.encode("ascii") + b"\n")
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Logging that cannot carry a secret
# --------------------------------------------------------------------------
class Log:
    """Writes one line at a time to stdout, redacting every secret value it has been told about."""

    def __init__(self, stream=None):
        self._stream = stream
        self._secrets: set = set()

    def add_secret(self, value: str | None) -> None:
        if value and len(value) >= 6:
            self._secrets.add(value)

    def __call__(self, line: str) -> None:
        for secret in self._secrets:
            line = line.replace(secret, "[REDACTED]")
        stream = self._stream or sys.stdout
        stream.write(line + "\n")
        stream.flush()


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class GitHubFetcher:
    """Downloads ``repos/{repo}/tarball/{sha}`` with one hand-followed, host-checked redirect.

    The Authorization header goes to api.github.com only. The redirect target
    (codeload.github.com, whose URL may itself carry a short-lived token) is
    requested with no Authorization header and is never logged.
    """

    def __init__(self, opener=None, log: Log | None = None):
        self._opener = opener or urllib.request.build_opener(_NoRedirect)
        self._log = log

    def __call__(self, repo: str, sha: str, token: str | None, dest: Path, max_bytes: int) -> None:
        url = f"https://{API_HOST}/repos/{repo}/tarball/{sha}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "whos-agentos-materialiser",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        self._stream_to(self._open(url, headers), dest, max_bytes)

    # internal -----------------------------------------------------------
    def _open(self, url: str, headers: dict):
        try:
            return self._opener.open(urllib.request.Request(url, headers=headers), timeout=FETCH_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as err:
            if err.code in (301, 302, 303, 307, 308):
                location = err.headers.get("Location") if err.headers else None
                err.close()
                return self._follow(location)
            code = err.code
            err.close()
            raise MaterialiseError(f"http_{code}") from None
        except _NET_ERRORS:
            raise MaterialiseError("network_error") from None

    def _follow(self, location: str | None):
        if not location:
            raise MaterialiseError("redirect_refused")
        parsed = urllib.parse.urlsplit(location)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != CODELOAD_HOST
            or parsed.username
            or parsed.password
        ):
            raise MaterialiseError("redirect_refused")
        if self._log is not None:
            self._log.add_secret(location)
            for _, values in urllib.parse.parse_qs(parsed.query).items():
                for value in values:
                    self._log.add_secret(value)
        try:
            return self._opener.open(
                urllib.request.Request(location, headers={"User-Agent": "whos-agentos-materialiser"}),
                timeout=FETCH_TIMEOUT_SECONDS,
            )
        except urllib.error.HTTPError as err:
            code = err.code
            err.close()
            raise MaterialiseError(f"http_{code}") from None
        except _NET_ERRORS:
            raise MaterialiseError("network_error") from None

    @staticmethod
    def _stream_to(response, dest: Path, max_bytes: int) -> None:
        total = 0
        try:
            with open(dest, "wb") as out:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise MaterialiseError("tarball_too_large")
                    out.write(chunk)
        except MaterialiseError:
            raise
        except _STREAM_ERRORS:
            raise MaterialiseError("network_error") from None
        finally:
            close = getattr(response, "close", None)
            if close:
                close()


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------
@dataclass
class ExtractResult:
    files: int = 0
    digest: str = ""
    entries: list = field(default_factory=list)  # (path, blob sha1)
    withheld: list = field(default_factory=list)  # paths only
    skipped_denied: int = 0
    skipped_binary: int = 0
    skipped_oversize: int = 0
    skipped_outside_allow: int = 0
    links_refused: int = 0


def extract_tarball(
    tar_path: Path,
    head: str,
    dest: Path | None,
    rules: DenyRules,
    limits: Limits = Limits(),
    path_allow: list | None = None,
) -> ExtractResult:
    """Extract allowed regular files of a GitHub tarball into ``dest`` (None = dry run).

    Raises MaterialiseError for anything that makes the archive untrustworthy as a
    whole: wrong commit, unsafe member name, layout conflict, bounds exceeded.
    Links, devices and other non-regular members are never extracted (counted).
    """

    result = ExtractResult()
    try:
        archive = tarfile.open(tar_path, mode="r:gz")
    except _ARCHIVE_ERRORS:
        raise MaterialiseError("bad_archive") from None
    seen: set = set()
    top: str | None = None
    declared = 0
    members = 0
    total_bytes = 0
    try:
        # GitHub writes the commit id in the pax global header comment. The top
        # directory name carries only 7 characters, so identity rests on this.
        comment = archive.pax_headers.get("comment") if archive.pax_headers else None
        if comment != head:
            raise MaterialiseError("commit_mismatch")
        for member in archive:
            members += 1
            if members > limits.max_members:
                raise MaterialiseError("too_many_members")
            name = member.name
            if "\0" in name or name.startswith("/") or (len(name) > 1 and name[1] == ":" and name[0].isalpha()):
                raise MaterialiseError("unsafe_member")
            parts = name.split("/")
            if parts and parts[-1] == "":
                parts = parts[:-1]
            if not parts or any(p in ("", ".", "..") for p in parts):
                raise MaterialiseError("unsafe_member")
            if top is None:
                top = parts[0]
            elif parts[0] != top:
                raise MaterialiseError("layout_unexpected")
            if member.isdir():
                continue
            if not member.isreg():
                # symlink, hard link, device, fifo: never created.
                result.links_refused += 1
                continue
            if len(parts) < 2:
                raise MaterialiseError("layout_unexpected")
            declared += max(member.size, 0)
            if declared > limits.max_unpacked_bytes:
                raise MaterialiseError("too_large")
            rel = "/".join(parts[1:])
            if rel in seen:
                raise MaterialiseError("duplicate_member")
            seen.add(rel)
            if deny_reason(rel, rules) is not None:
                result.skipped_denied += 1
                continue
            if path_allow and not path_allowed_by_prefix(rel, path_allow):
                result.skipped_outside_allow += 1
                continue
            if member.size > min(limits.max_file_bytes, rules.max_file_bytes):
                result.skipped_oversize += 1
                continue
            handle = archive.extractfile(member)
            if handle is None:
                result.links_refused += 1
                continue
            data = handle.read(member.size + 1)
            if len(data) != member.size:
                raise MaterialiseError("bad_archive")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                result.skipped_binary += 1
                continue
            if "\0" in text:
                result.skipped_binary += 1
                continue
            if carries_credential(text, rules.credential_scan_chars):
                result.withheld.append(rel)
                continue
            result.files += 1
            if result.files > limits.max_files:
                raise MaterialiseError("too_many_files")
            total_bytes += len(data)
            if total_bytes > limits.max_total_bytes:
                raise MaterialiseError("too_large")
            result.entries.append((rel, git_blob_sha1(data)))
            if dest is not None:
                _write_file(dest, rel, data)
    except MaterialiseError:
        raise
    except _ARCHIVE_ERRORS:
        raise MaterialiseError("bad_archive") from None
    finally:
        archive.close()
    result.digest = manifest_digest(result.entries)
    return result


def _write_file(dest: Path, rel: str, data: bytes) -> None:
    target = dest.joinpath(*rel.split("/"))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Stay inside dest even if a path component were somehow a link (we never create links).
        if os.path.realpath(target.parent) != os.path.realpath(dest) and not os.path.realpath(target.parent).startswith(
            os.path.realpath(dest) + os.sep
        ):
            raise MaterialiseError("unsafe_member")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except _CONFLICT_ERRORS:
        raise MaterialiseError("layout_conflict") from None
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _make_read_only(path: Path) -> None:
    """chmod -R a-w: files 0444, directories 0555 (children before parents)."""

    for current, _dirs, files in os.walk(path):
        for name in files:
            os.chmod(os.path.join(current, name), 0o444)
    for current, _dirs, _files in os.walk(path, topdown=False):
        os.chmod(current, 0o555)


def _force_remove(path: Path) -> None:
    if not os.path.lexists(path):
        return

    def _chmod_and_retry(func, target, _exc):
        try:
            os.chmod(os.path.dirname(target), stat.S_IRWXU)
            os.chmod(target, stat.S_IRWXU)
        except OSError:
            pass
        func(target)

    for current, dirs, _files in os.walk(path):
        try:
            os.chmod(current, stat.S_IRWXU)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_chmod_and_retry)


# --------------------------------------------------------------------------
# Pins
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RepoPin:
    repo: str
    head: str
    dir: str
    manifest_sha256: str
    path_allow: tuple
    private: bool
    token_selector: str


def parse_pins(raw: str) -> list:
    try:
        data = json.loads(raw)
    except ValueError:
        raise MaterialiseError("pins_invalid") from None
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict) or not repos:
        raise MaterialiseError("pins_invalid")
    pins = []
    for repo, spec in repos.items():
        if not isinstance(repo, str) or not _REPO_RE.match(repo) or not isinstance(spec, dict):
            raise MaterialiseError("pins_invalid")
        pins.append((repo, spec))
    return pins


def validate_pin(repo: str, spec: dict) -> RepoPin:
    if repo not in ALLOWED_REPOS:
        raise MaterialiseError("repo_not_allowed")
    head = spec.get("head")
    if not isinstance(head, str) or not _SHA_RE.match(head):
        raise MaterialiseError("head_invalid")
    digest = spec.get("manifest_sha256")
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        raise MaterialiseError("manifest_pin_missing")
    owner, name = repo.split("/", 1)
    expected_dir = f"{owner}__{name}__{head[:8]}"
    if spec.get("dir") != expected_dir:
        raise MaterialiseError("dir_invalid")
    path_allow = spec.get("path_allow") or []
    if not isinstance(path_allow, list) or not all(isinstance(p, str) and p for p in path_allow):
        raise MaterialiseError("pins_invalid")
    selector = spec.get("token") or ("ORG" if owner == ORG_OWNER else "USER")
    if selector not in TOKEN_SELECTORS:
        raise MaterialiseError("pins_invalid")
    return RepoPin(
        repo=repo,
        head=head,
        dir=expected_dir,
        manifest_sha256=digest,
        path_allow=tuple(path_allow),
        private=spec.get("private", True) is not False,
        token_selector=selector,
    )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def scrub_tokens(environ) -> int:
    names = [k for k in list(environ) if k.upper().startswith(TOKEN_PREFIX)]
    for name in names:
        del environ[name]
    return len(names)


def materialise(
    raw_pins: str | None,
    environ,
    root: Path,
    fetcher: Callable | None = None,
    log: Log | None = None,
    rules: DenyRules | None = None,
    limits: Limits = Limits(),
) -> int:
    """Run the whole job. Always returns 0 (see module docstring)."""

    log = log or Log()
    if raw_pins is None or not raw_pins.strip():
        log("WORKSPACE_SKIPPED reason=unset")
        return 0

    tokens = {sel: environ.get(TOKEN_PREFIX + sel) for sel in TOKEN_SELECTORS if environ.get(TOKEN_PREFIX + sel)}
    for value in tokens.values():
        log.add_secret(value)
    try:
        rules = rules or DenyRules.load()
        fetch = fetcher or GitHubFetcher(log=log)
        try:
            pins = parse_pins(raw_pins)
        except MaterialiseError as err:
            log(f"WORKSPACE_REFUSED repo=* reason={err.reason}")
            pins = []
        ready = []
        for repo, spec in pins:
            try:
                pin = validate_pin(repo, spec)
                try:
                    root.mkdir(parents=True, exist_ok=True)  # only once there is a valid pin to serve
                except OSError:
                    raise MaterialiseError("root_unwritable") from None
                ready.append((pin, _materialise_one(pin, tokens, root, fetch, rules, limits, log)))
            except MaterialiseError as err:
                log(f"WORKSPACE_REFUSED repo={repo} reason={err.reason}")
            except Exception:  # noqa: BLE001 - never leak an exception message (it could hold a URL)
                log(f"WORKSPACE_REFUSED repo={repo} reason=internal_error")
        if ready:
            _write_manifest_file(root, ready)
    except Exception:  # noqa: BLE001
        log("WORKSPACE_REFUSED repo=* reason=internal_error")
    finally:
        removed = scrub_tokens(environ)
        log(f"TOKEN_SCRUBBED removed={removed}")
    return 0


def _materialise_one(
    pin: RepoPin, tokens: dict, root: Path, fetch: Callable, rules: DenyRules, limits: Limits, log: Log
):
    token = tokens.get(pin.token_selector)
    if token is not None and not _TOKEN_RE.match(token):
        raise MaterialiseError("token_invalid")
    if token is None and pin.private:
        raise MaterialiseError("token_missing")
    final = root / pin.dir
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=root))
    try:
        tar_path = stage / "archive.tgz"
        fetch(pin.repo, pin.head, token, tar_path, limits.max_tarball_bytes)
        tree = stage / "tree"
        tree.mkdir()
        result = extract_tarball(tar_path, pin.head, tree, rules, limits, list(pin.path_allow))
        if result.digest != pin.manifest_sha256:
            raise MaterialiseError("manifest_mismatch")
        if result.files == 0:
            raise MaterialiseError("empty_tree")
        tar_path.unlink()
        _force_remove(final)
        os.rename(tree, final)
        _make_read_only(final)
    except Exception:
        # A refused repo is not exposed: remove any tree an earlier boot left at the same path.
        _force_remove(final)
        raise
    finally:
        _force_remove(stage)
    log(f"WORKSPACE_READY repo={pin.repo} head={pin.head[:8]} files={result.files} manifest=ok")
    return result


def _write_manifest_file(root: Path, ready: list) -> None:
    lines = []
    for pin, result in sorted(ready, key=lambda item: item[0].repo):
        lines.append(f"REPO {pin.repo} HEAD {pin.head} DIR {pin.dir} MANIFEST_SHA256 {result.digest}")
        for path, blob in sorted(result.entries):
            lines.append(f"{blob} {path}")
        for path in sorted(result.withheld):
            lines.append(f"WITHHELD_CREDENTIAL_SHAPED {path}")
    target = root / "MANIFEST.txt"
    if os.path.lexists(target):
        os.chmod(target, 0o644)
        target.unlink()
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(target, 0o444)


def _digest_only(argv: list) -> int:
    """Offline helper: print the manifest digest a pin must carry for a tarball the operator already holds."""

    import argparse

    parser = argparse.ArgumentParser(prog="materialise_workspaces.py --digest-only")
    parser.add_argument("tarball")
    parser.add_argument("--head", required=True)
    parser.add_argument("--path-allow", action="append", default=[])
    args = parser.parse_args(argv)
    if not _SHA_RE.match(args.head):
        print("head must be 40 lowercase hex characters", file=sys.stderr)
        return 2
    try:
        result = extract_tarball(Path(args.tarball), args.head, None, DenyRules.load(), Limits(), args.path_allow)
    except MaterialiseError as err:
        print(f"REFUSED reason={err.reason}")
        return 1
    print(f"MANIFEST_SHA256 {result.digest} files={result.files} withheld={len(result.withheld)}")
    return 0


def _purge(root: Path) -> int:
    """Remove everything under the workspaces root (used after a boot deadline killed a run mid-way)."""

    removed = 0
    if root.is_dir():
        for child in list(root.iterdir()):
            try:
                if child.is_dir() and not child.is_symlink():
                    _force_remove(child)
                else:
                    child.unlink()
                removed += 1
            except OSError:
                print("WORKSPACE_REFUSED repo=* reason=purge_failed")
    print(f"WORKSPACE_PURGED removed={removed}")
    return 0


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--digest-only":
        return _digest_only(argv[1:])
    if argv[:1] == ["--purge"]:
        return _purge(Path(argv[1]) if len(argv) >= 2 else DEFAULT_ROOT)
    root = DEFAULT_ROOT
    if argv[:1] == ["--root"] and len(argv) >= 2:
        root = Path(argv[1])
    return materialise(os.environ.get(PINS_ENV), os.environ, root)


if __name__ == "__main__":
    sys.exit(main())
