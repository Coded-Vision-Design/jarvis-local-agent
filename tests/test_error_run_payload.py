"""Regression: blocked tasks must expose reason/message for the cloud drawer."""
from __future__ import annotations

from src.runner import error_run_payload


def test_backend_failure_payload_includes_reason_and_message():
    p = error_run_payload(
        reason="claude_failed",
        message="claude-code exited 1",
        error="--dangerously-skip-permissions cannot be used with root",
    )
    assert p["reason"] == "claude_failed"
    assert "claude-code exited 1" in p["message"]
    assert p["summary"] == p["message"]
    assert "root" in p["error"]


def test_exception_payload_preserves_diagnosis_fields():
    p = error_run_payload(
        reason="git_fetch_failed",
        message="Git fetch failed → check SSH key",
        diagnosis="Git fetch failed",
        recommended_action="check SSH key",
        exception_type="CalledProcessError",
    )
    assert p["reason"] == "git_fetch_failed"
    assert p["diagnosis"] == "Git fetch failed"
