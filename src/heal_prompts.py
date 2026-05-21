"""Failure-class-specific steer prompts for backend retry + cross-backend
fallback.

The runner classifies a failed BackendResult into one of the classes below
and prepends the matching steer to the prompt on the next attempt. Same
backend + retry uses RETRY_STEER; cross-backend (claude → codex) uses
CROSS_BACKEND_STEER prepended on top.

Each steer is one short paragraph — long steers waste context and rarely
help."""
from __future__ import annotations

from typing import Literal

FailureClass = Literal[
    "rate_limited",
    "timeout",
    "empty_output",
    "crash",
    "auth_error",
    "unknown",
]

# Steers prepended on retry of the SAME backend after a same-backend failure.
RETRY_STEER: dict[str, str] = {
    "empty_output": (
        "Your previous attempt produced zero file changes and exited cleanly "
        "with no output. This usually means you stalled mid-plan or ran out of "
        "context. Be concrete: list the files you need to create or modify, "
        "then create them one at a time using Write/Edit tool calls. Do not "
        "describe the code in text — actually call the tools. The task is not "
        "complete until git diff shows real source files."
    ),
    "timeout": (
        "Your previous attempt timed out after no output for several minutes. "
        "The most common cause is getting stuck thinking. Start by listing "
        "the 3-5 concrete files you need to create or edit, then execute the "
        "first Write tool call immediately — do not plan beyond what you can "
        "ship in this single response."
    ),
    "crash": (
        "Your previous attempt crashed with a non-zero exit code. Treat this "
        "as a fresh start. Read the task brief carefully, identify the minimum "
        "shipable set of files, and create them one at a time. Avoid complex "
        "tool chains that might re-trigger the same crash."
    ),
    "unknown": (
        "Your previous attempt failed for an unclear reason. Treat this as a "
        "fresh start. Focus on the minimum shipable set of files needed to "
        "satisfy the task brief and create them one at a time."
    ),
}

# Steer prepended when SWITCHING backends mid-task (claude → codex etc.).
# Different tools, fresh context — keep the steer short.
CROSS_BACKEND_STEER_TEMPLATE = (
    "Another coding agent ({prev_backend}) tried this task and failed "
    "({prev_class}: {prev_summary}). You are a different tool with a fresh "
    "context. Focus on shipping concrete file changes that satisfy the task "
    "brief — do not retry the same approach that failed."
)


def classify_failure(
    summary: str | None,
    error: str | None,
    rate_limited: bool = False,
) -> FailureClass:
    """Map a BackendResult to a failure class.

    Order matters — rate_limited is checked first because the same line
    can match both rate-limit and timeout regexes (e.g. 'usage limit
    reached after 5 minutes')."""
    if rate_limited:
        return "rate_limited"
    haystack = f"{summary or ''}\n{error or ''}".lower()

    # Auth errors are operator-actionable; never retry.
    auth_markers = (
        "401 unauthorized",
        "invalid api key",
        "authentication failed",
        "bad credentials",
        "not authenticated",
        "anthropic_api_key not set",
        "openai_api_key not set",
    )
    if any(m in haystack for m in auth_markers):
        return "auth_error"

    # Backend-emitted timeout signals — we added one to claude.py
    # (rc=124 + "readline timeout") and codex has its own.
    if "readline timeout" in haystack or "timed out after" in haystack or "exit 124" in haystack:
        return "timeout"

    if "produced no output" in haystack or "no stdout captured" in haystack:
        return "empty_output"

    # Generic non-zero exit — treat as transient crash unless we matched
    # one of the more specific classes above.
    if "exited" in haystack and "exited 0" not in haystack:
        return "crash"

    return "unknown"


# Classes where retrying the SAME backend is sensible. Rate-limited and
# auth_error are explicitly excluded — they need operator action or a
# cooldown, not an immediate retry.
RETRYABLE_CLASSES: frozenset[FailureClass] = frozenset({
    "timeout",
    "empty_output",
    "crash",
    "unknown",
})


def is_retryable(cls: FailureClass) -> bool:
    return cls in RETRYABLE_CLASSES
