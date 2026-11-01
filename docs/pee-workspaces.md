# PEE pinned workspaces (WHOS PEE phase 1)

Read-only, pinned-commit copies of other repositories, materialised at container start so the `pee-reader` agent can read them. **Default off**: with `WHOS_WORKSPACE_PINS` unset nothing happens. Variable names only below; no value is stored in this repository.

| Variable | Where it is set | What it is |
|---|---|---|
| `WHOS_WORKSPACE_PINS` | AgentOS service (not secret) | JSON: per repo the full 40-hex commit, the directory name `<owner>__<repo>__<sha8>`, the manifest digest, optional `path_allow`, `private`, `token`. Unset or empty = off. |
| `WHOS_WORKSPACE_READ_TOKEN_ORG` | AgentOS service (secret) | Read-only token for repositories owned by `wise-hands-company`. Read once by `scripts/materialise_workspaces.py`, then removed. |
| `WHOS_WORKSPACE_READ_TOKEN_USER` | AgentOS service (secret) | Read-only token for repositories owned by `mohammadbashiranjum-ops`. Same handling. |

## Moving parts

- `scripts/materialise_workspaces.py` (standard library only) fetches `repos/{owner}/{repo}/tarball/{full_sha}`, checks the commit in the archive's pax header, extracts only allowed regular files into `/app/workspaces/<owner>__<repo>__<sha8>/`, recomputes the manifest digest (sha256 over sorted `path NUL git-blob-sha1 LF` lines) and compares it with the pin. Any failure removes that repo and logs `WORKSPACE_REFUSED repo=... reason=...`. It always exits 0.
- `scripts/pee_path_denylist_v1.json` is the one deny-list. Extraction drops matching files; `agents/pee_reader.py` passes the same patterns as `exclude_patterns`, concatenated with the library defaults.
- `scripts/entrypoint.sh` runs the materialiser, then unsets every `WHOS_WORKSPACE_READ_TOKEN_*` before `exec`.
- `app/token_guard.py`, called from `app/main.py` before the app is built, logs `TOKEN_ENV_CLEAN`, or `TOKEN_ENV_LEAK`, deletes the variables and exits 78 so the pins are never served from a process that carries a token.
- `agents/pee_reader.py` (`pee-reader`): read, list and search only; no learning, no history, no offload store.

## Boot log lines

`WORKSPACE_SKIPPED reason=unset` (off), `WORKSPACE_READY repo=... head=<sha8> files=<n> manifest=ok`, `WORKSPACE_REFUSED repo=... reason=<code>`, `TOKEN_SCRUBBED removed=<n>`, `TOKEN_SCRUBBED scope=entrypoint removed=<n>`, `TOKEN_ENV_CLEAN`, `TOKEN_ENV_LEAK count=<n> names=...`.

## Computing a pin's manifest digest

`python scripts/materialise_workspaces.py --digest-only <tarball> --head <40-hex> [--path-allow src/ ...]` prints `MANIFEST_SHA256 <hex> files=<n> withheld=<n>` for a tarball you already hold, using the same rules as the boot-time extraction.

## Not yet done here

Railway variables, the `agents:pee-reader:run` scope, the producer's `WHOS_AGENTOS_AGENT_ID`, and a deploy are Owner-gated (design Phase 2).
