"""Regression tests for the hive failover policy in runner.py.

Two layers of coverage:

  1. _is_retryable_infra_error — classifier that decides whether the
     exception should rotate to another worker (git/gh/network) or
     stop the line (application failure, test failure, etc.).

  2. End-to-end rotation simulation — feeds run_job() a task that will
     blow up inside _git_clone_or_fetch (because the GitHub remote
     doesn't resolve) and asserts the runner takes the rotation
     branch: merge_metadata({excluded_agents:[self], hive_retry_count:1})
     followed by set_status('queued') rather than set_status('blocked').
"""
from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import runner


# ── classifier ──────────────────────────────────────────────────────


def _cpe(cmd: list[str], stderr: str = "", returncode: int = 1) -> subprocess.CalledProcessError:
    exc = subprocess.CalledProcessError(returncode=returncode, cmd=cmd)
    exc.stderr = stderr
    return exc


def test_git_fetch_failure_is_retryable():
    exc = _cpe(["git", "fetch", "--prune", "origin"], "fatal: unable to access")
    assert runner._is_retryable_infra_error(exc) is True


def test_git_checkout_failure_is_retryable():
    exc = _cpe(["git", "checkout", "master"], "error: pathspec 'master' did not match")
    assert runner._is_retryable_infra_error(exc) is True


def test_gh_repo_create_failure_is_retryable():
    exc = _cpe(["gh", "repo", "create", "demo", "--private"], "HTTP 403")
    assert runner._is_retryable_infra_error(exc) is True


def test_full_path_git_command_is_retryable():
    """Some hosts shell out via absolute path; classifier must still
    recognise the executable basename."""
    exc = _cpe(["/usr/bin/git", "push", "origin", "main"])
    assert runner._is_retryable_infra_error(exc) is True


def test_npm_ci_failure_is_retryable():
    """`npm ci` registry blips are the canonical 'rotate me' case
    during build-and-test."""
    exc = _cpe(["npm", "ci"], "ETIMEDOUT")
    assert runner._is_retryable_infra_error(exc) is True


def test_dns_failure_is_retryable_even_for_unknown_cmd():
    exc = _cpe(["mystery-tool", "do-thing"], "could not resolve host github.com")
    assert runner._is_retryable_infra_error(exc) is True


def test_disk_full_is_retryable():
    exc = _cpe(["python", "build.py"], "no space left on device")
    assert runner._is_retryable_infra_error(exc) is True


def test_random_runtime_error_is_not_retryable():
    """A KeyError or AssertionError is a code bug; rotating workers
    won't help. Must stay terminal."""
    assert runner._is_retryable_infra_error(KeyError("nope")) is False
    assert runner._is_retryable_infra_error(AssertionError("boom")) is False
    assert runner._is_retryable_infra_error(RuntimeError("nope")) is False


def test_python_app_failure_is_not_retryable():
    """A test runner failure (pytest exit 1) is a real code defect.
    The classifier must NOT rotate, because the next worker will fail
    on the same test."""
    exc = _cpe(["pytest"], "1 failed, 0 passed")
    assert runner._is_retryable_infra_error(exc) is False


def test_calledprocesserror_with_empty_cmd_is_not_retryable():
    """Defensive: a CPE with no cmd shouldn't trigger rotation."""
    exc = subprocess.CalledProcessError(returncode=1, cmd=[])
    assert runner._is_retryable_infra_error(exc) is False


# ── rotation behaviour ─────────────────────────────────────────────


@pytest.fixture
def fake_client():
    """Async mock that records every method the runner calls so the
    rotation flow can be asserted in order."""
    c = MagicMock()
    c.append_run = AsyncMock()
    c.merge_metadata = AsyncMock()
    c.set_status = AsyncMock()
    c.heartbeat = AsyncMock()
    return c


@pytest.mark.asyncio
async def test_retryable_failure_rotates_instead_of_blocking(fake_client, monkeypatch):
    """End-to-end: when _git_clone_or_fetch raises a git CPE, run_job
    must merge_metadata({excluded_agents:[me],...}) and set_status('queued')
    — NOT set_status('blocked')."""
    task: dict[str, Any] = {
        "id": 7,
        "title": "Build a polished one-page demo website",
        "body": "If the repo doesn't exist yet, create it",
        "related_entity": "test-website",
        "metadata": {
            "delegated_to": "local-code-agent",
            "local_agent_backend": "smart",
            "local_agent_repo": "test-website",
            "local_agent_create_repo": True,
        },
    }

    boom = _cpe(["git", "fetch", "--prune", "origin"], "fatal: not found")

    with (
        patch.object(runner, "get_client", return_value=fake_client),
        patch.object(runner, "agent_id", return_value="workstation-test"),
        patch.object(runner, "can_run", return_value=(True, "")),
        patch.object(runner, "_git_clone_or_fetch", side_effect=boom),
        patch.object(runner, "post_as_jarvis", new=AsyncMock()),
        patch.object(runner, "fetch_prior_context", new=AsyncMock(return_value={})),
        patch.object(runner, "emit_handover_note", new=AsyncMock()),
    ):
        await runner.run_job(task)

    # The status flip must be 'queued', not 'blocked'.
    status_calls = [c.args for c in fake_client.set_status.call_args_list]
    assert any(args == (7, "queued") for args in status_calls), status_calls
    assert not any(args == (7, "blocked") for args in status_calls), status_calls

    # And the metadata must record the self-exclusion + retry count.
    md_calls = [c.args[1] for c in fake_client.merge_metadata.call_args_list]
    assert any(
        m.get("excluded_agents") == ["workstation-test"]
        and m.get("hive_retry_count") == 1
        for m in md_calls
    ), md_calls


@pytest.mark.asyncio
async def test_non_retryable_failure_still_blocks(fake_client):
    """A KeyError inside the backend isn't an infra error — the task
    must transition to 'blocked' as before, no rotation."""
    task: dict[str, Any] = {
        "id": 8,
        "title": "Add a feature",
        "body": "Add the feature please.",
        "related_entity": "test-website",
        "metadata": {
            "delegated_to": "local-code-agent",
            "local_agent_backend": "smart",
            "local_agent_repo": "test-website",
        },
    }

    with (
        patch.object(runner, "get_client", return_value=fake_client),
        patch.object(runner, "agent_id", return_value="workstation-test"),
        patch.object(runner, "can_run", return_value=(True, "")),
        patch.object(runner, "_git_clone_or_fetch", side_effect=KeyError("config")),
        patch.object(runner, "post_as_jarvis", new=AsyncMock()),
        patch.object(runner, "fetch_prior_context", new=AsyncMock(return_value={})),
        patch.object(runner, "emit_handover_note", new=AsyncMock()),
    ):
        await runner.run_job(task)

    status_calls = [c.args for c in fake_client.set_status.call_args_list]
    assert any(args == (8, "blocked") for args in status_calls), status_calls
    assert not any(args == (8, "queued") for args in status_calls), status_calls
    # Nothing should have been written to excluded_agents.
    md_calls = [c.args[1] for c in fake_client.merge_metadata.call_args_list]
    assert not any("excluded_agents" in m for m in md_calls), md_calls


@pytest.mark.asyncio
async def test_retry_cap_eventually_blocks(fake_client):
    """After HIVE_RETRY_MAX rotations, the runner should give up and
    block so the operator is paged instead of the queue thrashing."""
    task: dict[str, Any] = {
        "id": 9,
        "title": "blah",
        "body": "blah",
        "related_entity": "test-website",
        "metadata": {
            "delegated_to": "local-code-agent",
            "local_agent_backend": "smart",
            "local_agent_repo": "test-website",
            # Already at the cap — next infra failure must block.
            "hive_retry_count": runner.HIVE_RETRY_MAX,
            "excluded_agents": ["worker-a", "worker-b", "worker-c"],
        },
    }

    boom = _cpe(["git", "fetch", "--prune", "origin"], "fatal: not found")

    with (
        patch.object(runner, "get_client", return_value=fake_client),
        patch.object(runner, "agent_id", return_value="worker-d"),
        patch.object(runner, "can_run", return_value=(True, "")),
        patch.object(runner, "_git_clone_or_fetch", side_effect=boom),
        patch.object(runner, "post_as_jarvis", new=AsyncMock()),
        patch.object(runner, "fetch_prior_context", new=AsyncMock(return_value={})),
        patch.object(runner, "emit_handover_note", new=AsyncMock()),
    ):
        await runner.run_job(task)

    status_calls = [c.args for c in fake_client.set_status.call_args_list]
    assert any(args == (9, "blocked") for args in status_calls), status_calls
    assert not any(args == (9, "queued") for args in status_calls), status_calls


@pytest.mark.asyncio
async def test_already_excluded_agent_does_not_loop(fake_client):
    """Defensive: if for some reason the SQL gate missed and this worker
    is already in excluded_agents, the runner must hard-block instead of
    self-exclude-and-re-queue-forever."""
    task: dict[str, Any] = {
        "id": 10,
        "title": "blah",
        "body": "blah",
        "related_entity": "test-website",
        "metadata": {
            "delegated_to": "local-code-agent",
            "local_agent_backend": "smart",
            "local_agent_repo": "test-website",
            "hive_retry_count": 1,
            "excluded_agents": ["workstation-test"],  # self
        },
    }

    boom = _cpe(["git", "fetch", "--prune", "origin"], "fatal: not found")

    with (
        patch.object(runner, "get_client", return_value=fake_client),
        patch.object(runner, "agent_id", return_value="workstation-test"),
        patch.object(runner, "can_run", return_value=(True, "")),
        patch.object(runner, "_git_clone_or_fetch", side_effect=boom),
        patch.object(runner, "post_as_jarvis", new=AsyncMock()),
        patch.object(runner, "fetch_prior_context", new=AsyncMock(return_value={})),
        patch.object(runner, "emit_handover_note", new=AsyncMock()),
    ):
        await runner.run_job(task)

    status_calls = [c.args for c in fake_client.set_status.call_args_list]
    assert any(args == (10, "blocked") for args in status_calls), status_calls
    assert not any(args == (10, "queued") for args in status_calls), status_calls
