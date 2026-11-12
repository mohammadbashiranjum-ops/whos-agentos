"""
Prompt Budget
=============

Pure helpers that keep one agent run under a free-tier per-request token ceiling.

Why: inside a run agno appends every tool result to the same message list, so each later
step resends all earlier results. One uncapped tool result (a recursive listing, a wide
search) takes a ~1.6K-token step to ~19K and a free lane that allows 8,000 TPM / 7,000 ITPM
refuses it. These helpers cap each tool result at a fixed size and read the three knobs
from the environment, so a route with a bigger context raises them by env, not by code.

No agno import: this module is unit-testable on its own.

Env (all optional; unset, garbage or out-of-range falls back to the default or the clamp):
  PEE_READER_TOOL_RESULT_MAX_TOKENS   default 700,  clamp 200..3000
  PEE_READER_TOOL_CALL_LIMIT          default 3,    clamp 1..8
  PEE_READER_MAX_COMPLETION_TOKENS    default 2048, clamp 128..4096
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from math import ceil
from os import getenv
from typing import Any

# Conservative for code and line-number-prefixed text; chars/4 under-estimates it.
CHARS_PER_TOKEN = 3

TOOL_RESULT_MAX_TOKENS_ENV = "PEE_READER_TOOL_RESULT_MAX_TOKENS"
TOOL_CALL_LIMIT_ENV = "PEE_READER_TOOL_CALL_LIMIT"
MAX_COMPLETION_TOKENS_ENV = "PEE_READER_MAX_COMPLETION_TOKENS"

DEFAULT_TOOL_RESULT_MAX_TOKENS = 700
DEFAULT_TOOL_CALL_LIMIT = 3
DEFAULT_MAX_COMPLETION_TOKENS = 2048

TOOL_RESULT_MAX_TOKENS_RANGE = (200, 3000)
TOOL_CALL_LIMIT_RANGE = (1, 8)
MAX_COMPLETION_TOKENS_RANGE = (128, 4096)

# Worst-case request budget the permanent guard test enforces (Groq free: 8,000 TPM / 7,000 ITPM).
REQUEST_BUDGET_TOKENS = 6100


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: ceil(len / 3)."""
    return ceil(len(text) / CHARS_PER_TOKEN)


def _marker(shown: int, total: int) -> str:
    return f"\n[TRUNCATED: showing {shown} of {total} lines; narrow the range or path and call again]"


def cap_text(text: Any, max_tokens: int) -> tuple[Any, bool]:
    """Return ``(text, truncated)`` with ``text`` at most ``max_tokens`` estimated tokens.

    Keeps the head, cuts on a line boundary and appends a fixed marker that is counted inside
    the cap. Idempotent. Never raises: non-str input is returned unchanged. A first line longer
    than the cap is hard-cut, so the cap always holds.
    """
    if not isinstance(text, str):
        return text, False
    limit = max(int(max_tokens), 1) * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text, False
    total = text.count("\n") + 1
    budget = limit - len(_marker(total, total))  # the real marker is never longer than this one
    if budget <= 0:
        return text[:limit], True
    head = text[:budget]
    cut = head.rfind("\n")
    if cut > 0:
        head = head[:cut]
    shown = head.count("\n") + 1 if head else 0
    return (head + _marker(shown, total))[:limit], True


def _env_int(name: str, default: int, bounds: tuple[int, int]) -> int:
    raw = getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    if value <= 0:
        return default
    return min(max(value, bounds[0]), bounds[1])


def tool_result_max_tokens() -> int:
    return _env_int(TOOL_RESULT_MAX_TOKENS_ENV, DEFAULT_TOOL_RESULT_MAX_TOKENS, TOOL_RESULT_MAX_TOKENS_RANGE)


def tool_call_limit() -> int:
    return _env_int(TOOL_CALL_LIMIT_ENV, DEFAULT_TOOL_CALL_LIMIT, TOOL_CALL_LIMIT_RANGE)


def max_completion_tokens() -> int:
    return _env_int(MAX_COMPLETION_TOKENS_ENV, DEFAULT_MAX_COMPLETION_TOKENS, MAX_COMPLETION_TOKENS_RANGE)


def cap_entrypoint(
    fn: Callable[..., Any],
    max_tokens: int,
    *,
    before: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    after: Callable[[Any], Any] | None = None,
) -> Callable[..., Any]:
    """Wrap a tool entrypoint so its string result is capped; sync and async get the matching wrapper.

    ``before`` receives the bound arguments by name and returns the (possibly clamped) arguments;
    ``after`` reshapes the raw result before the text cap. The wrapper keeps the name, docstring
    and signature, so the model-facing tool schema is unchanged.
    """
    signature = inspect.signature(fn)

    def _prepare(args: tuple, kwargs: dict) -> dict[str, Any]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        return before(arguments) if before else arguments

    def _finish(result: Any) -> Any:
        if after:
            result = after(result)
        return cap_text(result, max_tokens)[0]

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def acapped(*args: Any, **kwargs: Any) -> Any:
            return _finish(await fn(**_prepare(args, kwargs)))

        return acapped

    @functools.wraps(fn)
    def capped(*args: Any, **kwargs: Any) -> Any:
        return _finish(fn(**_prepare(args, kwargs)))

    return capped
