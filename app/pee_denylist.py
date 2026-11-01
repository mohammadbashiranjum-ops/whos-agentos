"""
PEE Path Deny-List
==================

Reads the one shared deny-list, `scripts/pee_path_denylist_v1.json`, and turns it
into the `exclude_patterns` the workspace provider takes. The same file drives
extraction in `scripts/materialise_workspaces.py`, so the boundary at extraction
and the provider's defence in depth cannot drift apart.

The provider matches one path component at a time, case-sensitively on Linux, and
patterns may not contain a path separator. So each alphabetic character becomes a
two-letter class (`*.pem` -> `*.[pP][eE][mM]`): `KEY.PEM` is then matched as well.
"""

import json
from pathlib import Path

DENYLIST_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pee_path_denylist_v1.json"


def case_insensitive(pattern: str) -> str:
    out = []
    for ch in pattern:
        if ch.isascii() and ch.isalpha():
            out.append(f"[{ch.lower()}{ch.upper()}]")
        else:
            out.append(ch)
    return "".join(out)


def load_denylist(path: Path = DENYLIST_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def provider_exclude_patterns(for_agent: str, path: Path = DENYLIST_PATH) -> list[str]:
    """Segment patterns for the provider. ``for_agent`` is 'pee-reader' or 'platform-engineer'."""

    data = load_denylist(path)
    patterns = [case_insensitive(p) for p in data["segment_patterns"]]
    if for_agent == "pee-reader":
        patterns += [case_insensitive(p) for p in data["provider_only_patterns"]]
    elif for_agent == "platform-engineer":
        patterns += list(data["engineer_extra_patterns"])
    else:
        raise ValueError(f"unknown agent {for_agent!r}")
    for pattern in patterns:
        if "/" in pattern or "\\" in pattern:
            raise ValueError("provider patterns match one component and cannot hold a separator")
    return patterns
