"""Regression tests for `_git_clone_or_fetch` self-healing.

Reproduces the bug pattern that left the `test-website` task perma-blocked:
the local workspace had a `main` branch with one commit, but the GitHub
remote was empty (no refs), so `git reset --hard origin/main` exit-128'd
and the legacy `master` fallback also failed with a confusing pathspec
error.

These tests stand up real local-only bare repos as "remotes" so we
exercise the actual git binary, not mocked subprocess output.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src import runner
from src.config import settings


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_bare(parent: Path, name: str, initial_branch: str = "main") -> Path:
    """Create an empty bare repo with no commits and no refs."""
    bare = parent / f"{name}.git"
    _run(["git", "init", "--bare", "--initial-branch", initial_branch, str(bare)])
    return bare


def _seed_bare(bare: Path, branch: str = "main") -> None:
    """Seed a bare repo with one commit on the given branch."""
    work = bare.parent / f"{bare.stem}-seed"
    _run(["git", "clone", str(bare), str(work)])
    _run(["git", "config", "user.email", "t@test"], cwd=work)
    _run(["git", "config", "user.name", "t"], cwd=work)
    _run(["git", "checkout", "-B", branch], cwd=work)
    (work / "README.md").write_text("seed", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=work)
    _run(["git", "commit", "-m", "seed"], cwd=work)
    _run(["git", "push", "-u", "origin", branch], cwd=work)
    shutil.rmtree(work, ignore_errors=True)


@pytest.fixture
def patched_settings(tmp_path, monkeypatch):
    """Point the runner at a throwaway workspace_root and stub `_origin_url`
    so it returns a local file:// URL we control (no GitHub roundtrip)."""
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "ws")
    (tmp_path / "ws" / "workspaces").mkdir(parents=True, exist_ok=True)

    remotes_root = tmp_path / "remotes"
    remotes_root.mkdir(parents=True, exist_ok=True)

    def fake_origin_url(repo: str) -> str:
        return (remotes_root / f"{repo}.git").as_uri()

    monkeypatch.setattr(runner, "_origin_url", fake_origin_url)
    return remotes_root


# ---------------------------------------------------------------------------
# The exact bug from the task: local main exists, remote has zero refs.
# Before the fix this raised `CalledProcessError: git checkout master`.
# After the fix the runner seeds the empty remote with the local commit.
# ---------------------------------------------------------------------------
def test_empty_remote_with_local_commits_seeds_the_remote(patched_settings, tmp_path):
    repo = "demo-empty-remote"
    bare = _make_bare(patched_settings, repo)  # empty, no refs

    # Stage a half-bootstrapped local workspace: local main with one commit,
    # but origin/main does not exist remotely. Exactly the test-website state.
    target = settings.workspace_root / "workspaces" / repo
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "--initial-branch=main", str(target)])
    _run(["git", "remote", "add", "origin", bare.as_uri()], cwd=target)
    _run(["git", "config", "user.email", "t@test"], cwd=target)
    _run(["git", "config", "user.name", "t"], cwd=target)
    (target / "README.md").write_text("local", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=target)
    _run(["git", "commit", "-m", "local-only"], cwd=target)

    out = runner._git_clone_or_fetch(repo)

    assert out == target
    # The seed push must have landed: the bare repo now advertises main.
    ls = subprocess.run(
        ["git", "ls-remote", "--heads", bare.as_uri(), "main"],
        capture_output=True, text=True, check=True,
    )
    assert "refs/heads/main" in ls.stdout


# ---------------------------------------------------------------------------
# Healthy path: remote has main, local workspace already cloned.
# Verifies the fix didn't regress the common case.
# ---------------------------------------------------------------------------
def test_healthy_remote_resets_to_main(patched_settings, tmp_path):
    repo = "demo-healthy"
    bare = _make_bare(patched_settings, repo)
    _seed_bare(bare, "main")

    target = settings.workspace_root / "workspaces" / repo
    _run(["git", "clone", bare.as_uri(), str(target)])

    # Drift the local main with an extra commit that should be reset away.
    _run(["git", "config", "user.email", "t@test"], cwd=target)
    _run(["git", "config", "user.name", "t"], cwd=target)
    (target / "drift.txt").write_text("drift", encoding="utf-8")
    _run(["git", "add", "drift.txt"], cwd=target)
    _run(["git", "commit", "-m", "drift"], cwd=target)

    out = runner._git_clone_or_fetch(repo)

    assert out == target
    assert not (target / "drift.txt").exists(), "reset --hard should have wiped drift"
    # Still on main, not detached.
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(target), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "main"


# ---------------------------------------------------------------------------
# Legacy default branch named `master`. The old code happened to handle this
# correctly because of the try/except fallback; we need to confirm the new
# code (which resolves the default via origin/HEAD) still does.
# ---------------------------------------------------------------------------
def test_master_default_branch_still_works(patched_settings, tmp_path):
    repo = "demo-master"
    bare = _make_bare(patched_settings, repo, initial_branch="master")
    _seed_bare(bare, "master")

    target = settings.workspace_root / "workspaces" / repo
    _run(["git", "clone", bare.as_uri(), str(target)])

    out = runner._git_clone_or_fetch(repo)

    assert out == target
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(target), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "master"


# ---------------------------------------------------------------------------
# Local workspace doesn't exist yet → falls through to a fresh clone.
# ---------------------------------------------------------------------------
def test_no_local_workspace_fresh_clones(patched_settings, tmp_path):
    repo = "demo-fresh"
    bare = _make_bare(patched_settings, repo)
    _seed_bare(bare, "main")

    target = settings.workspace_root / "workspaces" / repo
    assert not target.exists()

    out = runner._git_clone_or_fetch(repo)

    assert out == target
    assert (target / ".git").exists()
    assert (target / "README.md").exists()


# ---------------------------------------------------------------------------
# `_remote_default_branch` returns None when the remote is empty AND there
# are no remote-tracking refs locally. This is the precondition for the
# self-heal branch we added.
# ---------------------------------------------------------------------------
def test_remote_default_branch_none_for_empty_remote(patched_settings, tmp_path):
    repo = "demo-default-none"
    bare = _make_bare(patched_settings, repo)  # empty
    target = settings.workspace_root / "workspaces" / repo
    _run(["git", "clone", bare.as_uri(), str(target)])
    # `git clone` against an empty remote leaves no remote refs at all.
    assert runner._remote_default_branch(target) is None


def test_remote_default_branch_resolves_main(patched_settings, tmp_path):
    repo = "demo-default-main"
    bare = _make_bare(patched_settings, repo)
    _seed_bare(bare, "main")
    target = settings.workspace_root / "workspaces" / repo
    _run(["git", "clone", bare.as_uri(), str(target)])
    assert runner._remote_default_branch(target) == "main"


def test_remote_default_branch_resolves_master(patched_settings, tmp_path):
    repo = "demo-default-master"
    bare = _make_bare(patched_settings, repo, initial_branch="master")
    _seed_bare(bare, "master")
    target = settings.workspace_root / "workspaces" / repo
    _run(["git", "clone", bare.as_uri(), str(target)])
    assert runner._remote_default_branch(target) == "master"


def test_fetch_128_retries_after_origin_refresh(patched_settings, tmp_path, monkeypatch):
    """Stale PAT in origin URL must not block the task on the first 128.

    Reproduces the 09:29 test-website failure mode: fetch dies with 128
    when origin still embeds a revoked token, even though the current env
    token is fine. The runner should refresh origin and retry fetch before
    nuking the workspace.
    """
    repo = "demo-fetch-retry"
    bare = _make_bare(patched_settings, repo)
    _seed_bare(bare, "main")

    target = settings.workspace_root / "workspaces" / repo
    _run(["git", "clone", bare.as_uri(), str(target)])

    fetch_calls = {"n": 0}
    real_sh = runner._sh

    def fake_sh(cmd, cwd=None, check=True):
        if cmd[:3] == ["git", "fetch", "--prune"]:
            fetch_calls["n"] += 1
            if fetch_calls["n"] == 1:
                raise subprocess.CalledProcessError(
                    128,
                    cmd,
                    "",
                    "fatal: Authentication failed",
                )
        return real_sh(cmd, cwd=cwd, check=check)

    monkeypatch.setattr(runner, "_sh", fake_sh)

    out = runner._git_clone_or_fetch(repo)

    assert out == target
    assert fetch_calls["n"] == 2
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(target),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == "main"
