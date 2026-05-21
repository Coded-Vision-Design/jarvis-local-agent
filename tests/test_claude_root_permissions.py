"""Regression: Claude Code refuses --dangerously-skip-permissions as root (uid 0).

Reproduced in the jarvis-agent container:
  claude -p x ... --dangerously-skip-permissions
  → exit 1, stderr: cannot be used with root/sudo privileges

Task #7 at 01:44 blocked in ~11s with empty UI error because every model tier
failed instantly with rc=1 and the drawer only reads reason/message fields.
"""
from __future__ import annotations

from unittest.mock import patch

from src.backends import claude as claude_mod
from src.backends.claude import claude_headless_flags


def test_headless_flags_include_skip_permissions_when_not_root():
    with patch.object(claude_mod.os, "name", "posix"), patch.object(
        claude_mod.os, "getuid", return_value=1000, create=True
    ):
        flags = claude_headless_flags()
    assert "--dangerously-skip-permissions" in flags


def test_headless_flags_omit_skip_permissions_when_root():
    with patch.object(claude_mod.os, "name", "posix"), patch.object(
        claude_mod.os, "getuid", return_value=0, create=True
    ):
        flags = claude_headless_flags()
    assert "--dangerously-skip-permissions" not in flags
    assert "--verbose" in flags
    assert "--output-format" in flags


def test_root_skip_permissions_stderr_is_documented_failure_mode():
    """Sanity anchor — if Claude changes this message, update claude.py + this test."""
    msg = "--dangerously-skip-permissions cannot be used with root/sudo privileges"
    assert "root" in msg and "dangerously-skip-permissions" in msg
