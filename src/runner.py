"""Runs a single delegated task end-to-end:
clone/fetch → branch → sub-agent → commit → push → PR → webhook → status=done.

Heartbeats every HEARTBEAT_INTERVAL_SECONDS so the reaper doesn't evict.
On any unhandled exception, sets status=blocked and posts to Discord."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from .backends import get_backend
from .config import settings
from .discord_webhook import post_as_jarvis
from .jarvis_client import get_client
from .repos import is_whitelisted
from .state import slots

log = logging.getLogger("jarvis-agent.runner")

GITHUB_ORG = "codedvisiondesign"  # matches CLAUDE.md / .env defaults


def _slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:n] or "task"


def _sh(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Synchronous shell helper — git/gh are quick and we want the simple API."""
    log.debug("$ %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        capture_output=True,
        text=True,
    )


def _git_clone_or_fetch(repo: str) -> Path:
    """Ensure the workspace exists and is on origin/main. Returns the path."""
    target = settings.workspace_root / "workspaces" / repo
    target.parent.mkdir(parents=True, exist_ok=True)
    if not (target / ".git").exists():
        url = f"git@github.com:{GITHUB_ORG}/{repo}.git"
        _sh(["git", "clone", url, str(target)])
    else:
        _sh(["git", "fetch", "--prune", "origin"], cwd=target)
        # Hard-reset main to origin/main so we always start fresh.
        # If there's no main, try master.
        try:
            _sh(["git", "checkout", "main"], cwd=target)
            _sh(["git", "reset", "--hard", "origin/main"], cwd=target)
        except subprocess.CalledProcessError:
            _sh(["git", "checkout", "master"], cwd=target)
            _sh(["git", "reset", "--hard", "origin/master"], cwd=target)
    return target


def _make_branch(workspace: Path, slug: str) -> str:
    name = f"jarvis/{slug}-{int(time.time())}"
    _sh(["git", "checkout", "-b", name], cwd=workspace)
    return name


def _has_uncommitted_changes(workspace: Path) -> bool:
    r = _sh(["git", "status", "--porcelain"], cwd=workspace)
    return bool(r.stdout.strip())


def _commit_and_push(workspace: Path, branch: str, summary: str) -> None:
    _sh(["git", "add", "-A"], cwd=workspace)
    _sh(
        [
            "git",
            "-c", "user.email=contact@codedvisiondesign.co.uk",
            "-c", "user.name=Coded Vision Design",
            "commit",
            "-m", summary[:72] if summary else "jarvis-local-agent: delegated change",
        ],
        cwd=workspace,
    )
    _sh(["git", "push", "-u", "origin", branch], cwd=workspace)


def _open_pr(workspace: Path, title: str, body: str) -> str | None:
    """Open a PR via gh. Returns the URL on success."""
    try:
        r = _sh(
            ["gh", "pr", "create", "--title", title[:200], "--body", body[:60_000]],
            cwd=workspace,
        )
        url = r.stdout.strip().splitlines()[-1] if r.stdout else ""
        return url if url.startswith("https://") else None
    except subprocess.CalledProcessError as e:
        log.warning("gh pr create failed: %s", e.stderr)
        return None


async def _heartbeat_loop(task_id: int, stop: asyncio.Event) -> None:
    interval = settings.heartbeat_interval_seconds
    client = get_client()
    while not stop.is_set():
        try:
            await client.heartbeat(task_id)
        except Exception:
            log.exception("heartbeat error (ignored)")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def run_job(task: dict[str, Any]) -> None:
    """Entry point — already-claimed task. Always returns; never raises out."""
    task_id = int(task["id"])
    metadata = task.get("metadata") or {}
    backend_name = metadata.get("local_agent_backend") or "qwen"
    repo = metadata.get("local_agent_repo") or task.get("related_entity")
    body = task.get("body") or task.get("title", "")
    title_slug = _slug(task.get("title") or "task")
    client = get_client()

    if not repo or not is_whitelisted(str(repo)):
        await client.append_run(
            task_id,
            "error",
            {"message": f"repo {repo!r} not whitelisted; refusing to run"},
        )
        await client.set_status(task_id, "blocked")
        await post_as_jarvis(
            f"❌ Task #{task_id} blocked: repo `{repo}` isn't whitelisted in `C:\\Jarvis\\agent\\repos.yml`."
        )
        return

    sem = slots.acquire(backend_name)
    if sem.locked():
        # Another job of this backend is in flight — put us back in the queue.
        await client.append_run(
            task_id, "jarvis_note",
            {"message": f"{backend_name} slot busy; requeuing"},
        )
        await client.set_status(task_id, "queued")
        return

    stop_hb = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat_loop(task_id, stop_hb))

    branch: str | None = None
    workspace: Path | None = None
    started = time.time()

    try:
        async with sem:
            await client.append_run(task_id, "jarvis_note", {
                "message": f"claimed by jarvis-local-agent; backend={backend_name}; repo={repo}",
            })

            # 1. Workspace
            workspace = await asyncio.to_thread(_git_clone_or_fetch, str(repo))
            branch = await asyncio.to_thread(_make_branch, workspace, title_slug)
            await client.append_run(task_id, "jarvis_note", {
                "message": f"working in {workspace} on branch {branch}",
            })

            # 2. Run backend
            backend = get_backend(backend_name)

            async def log_cb(kind: str, payload: dict) -> None:
                await client.append_run(task_id, kind, payload)

            result = await backend.run(body, workspace, log_cb)

            # 3. Commit / push / PR (if there are changes)
            if not result.ok:
                await client.append_run(task_id, "error", {
                    "summary": result.summary,
                    "error": result.error or "",
                })
                await client.set_status(task_id, "blocked")
                await post_as_jarvis(
                    f"⚠️ Task #{task_id} ({backend_name}, {repo}) failed: {result.summary[:300]}"
                )
                return

            changed = await asyncio.to_thread(_has_uncommitted_changes, workspace)
            if not changed:
                await client.append_run(task_id, "jarvis_note", {
                    "message": "sub-agent finished with no file changes",
                })
                await client.set_status(
                    task_id, "done",
                    spent_pence=result.spent_pence,
                    spent_tokens=result.spent_tokens,
                )
                elapsed = int(time.time() - started)
                await post_as_jarvis(
                    f"ℹ️ Task #{task_id} ({backend_name}, `{repo}`) done — no changes needed. _{elapsed}s_"
                )
                return

            commit_msg = (task.get("title") or "jarvis: delegated change").split("\n")[0]
            await asyncio.to_thread(_commit_and_push, workspace, branch, commit_msg)

            pr_body_parts = [
                f"Delegated by Jarvis (task #{task_id}, backend={backend_name}).",
                "",
                f"**Request:**\n\n{body[:4000]}",
                "",
                f"**Sub-agent summary:**\n\n{result.summary[:6000]}",
            ]
            pr_url = await asyncio.to_thread(
                _open_pr, workspace, commit_msg, "\n".join(pr_body_parts)
            )

            if pr_url:
                await client.merge_metadata(task_id, {
                    "local_agent_pr_url": pr_url,
                    "local_agent_branch": branch,
                })

            await client.set_status(
                task_id, "done",
                spent_pence=result.spent_pence,
                spent_tokens=result.spent_tokens,
            )

            elapsed = int(time.time() - started)
            mins, secs = divmod(elapsed, 60)
            elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            msg = (
                f"✅ **Task #{task_id}** ({backend_name}, `{repo}`)\n"
                f"Branch `{branch}` · {elapsed_str}"
            )
            if pr_url:
                msg += f" · PR <{pr_url}>"
            await post_as_jarvis(msg)

    except Exception as e:
        log.exception("run_job crashed")
        await client.append_run(task_id, "error", {
            "message": "runner exception",
            "exception": str(e),
        })
        await client.set_status(task_id, "blocked")
        await post_as_jarvis(
            f"💥 Task #{task_id} crashed in the local agent: `{type(e).__name__}: {str(e)[:200]}`"
        )
    finally:
        stop_hb.set()
        await hb_task
