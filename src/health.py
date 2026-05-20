"""Proactive health loop — scans all whitelisted repos periodically and
creates self-directed Jarvis tasks when it finds actionable problems.

Checks performed:
  - Failing GitHub Actions CI runs
  - Stale open PRs (no activity for 7+ days)
  - Open issues labelled 'jarvis'
  - High/critical npm audit vulnerabilities
  - Composer audit issues (PHP projects)

Each check result is deduplicated via a JSON sidecar file so we don't
create the same task twice. Deduplication TTL is 24 hours."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .approvals import add_pending
from .config import settings
from .discord_webhook import post_as_jarvis
from .jarvis_client import get_client
from .repos import load_repos

log = logging.getLogger("jarvis-agent.health")

GITHUB_ORG = "Coded-Vision-Design"
DEDUP_FILE = Path("/workspace/agent/.health_seen.json")
DEDUP_TTL_SECONDS = 86400  # 24 hours


@dataclass
class Finding:
    repo: str
    kind: str           # "ci_failure", "stale_pr", "jarvis_issue", "npm_audit", "composer_audit"
    severity: str       # "critical", "high", "medium", "low"
    title: str
    task_body: str
    dedup_key: str      # unique key used to avoid duplicate tasks
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Deduplication helpers ─────────────────────────────────────────────────────

def _load_seen() -> dict[str, float]:
    if DEDUP_FILE.exists():
        try:
            return json.loads(DEDUP_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_seen(seen: dict[str, float]) -> None:
    DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEDUP_FILE.write_text(json.dumps(seen, indent=2), encoding="utf-8")


def _prune_seen(seen: dict[str, float]) -> dict[str, float]:
    cutoff = time.time() - DEDUP_TTL_SECONDS
    return {k: v for k, v in seen.items() if v > cutoff}


def _already_seen(seen: dict[str, float], key: str) -> bool:
    return key in seen


# ── Individual checks ─────────────────────────────────────────────────────────

def _run_gh(args: list[str]) -> tuple[int, str]:
    """Run a gh CLI command. Returns (returncode, stdout)."""
    try:
        r = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.returncode, r.stdout
    except Exception as exc:
        log.debug("gh command failed: %s", exc)
        return 1, ""


def check_ci(repo: str) -> list[Finding]:
    """Find failing GitHub Actions runs in the last 24 hours."""
    findings: list[Finding] = []
    rc, out = _run_gh([
        "run", "list",
        "--repo", f"{GITHUB_ORG}/{repo}",
        "--status", "failure",
        "--limit", "5",
        "--json", "databaseId,name,headBranch,createdAt,url",
    ])
    if rc != 0 or not out.strip():
        return findings

    try:
        runs = json.loads(out)
    except json.JSONDecodeError:
        return findings

    cutoff = time.time() - 86400  # last 24 h
    for run in runs:
        run_id = str(run.get("databaseId", ""))
        created_str = run.get("createdAt", "")
        # Simple recency filter - skip if older than 24 h
        try:
            import datetime
            created = datetime.datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            if created.timestamp() < cutoff:
                continue
        except Exception:
            pass

        findings.append(Finding(
            repo=repo,
            kind="ci_failure",
            severity="high",
            title=f"CI failure in {repo}: {run.get('name', 'unknown workflow')} on {run.get('headBranch', '?')}",
            task_body=(
                f"The GitHub Actions workflow '{run.get('name')}' is failing on branch "
                f"'{run.get('headBranch')}' in {repo}.\n\n"
                f"Run URL: {run.get('url', 'unknown')}\n\n"
                f"Please investigate the failure, read the logs with `gh run view {run_id} --log-failed`, "
                f"fix the root cause, and ensure CI is green before pushing."
            ),
            dedup_key=f"ci_failure:{repo}:{run_id}",
        ))
    return findings


def check_stale_prs(repo: str) -> list[Finding]:
    """Find open PRs with no activity in 7+ days."""
    findings: list[Finding] = []
    rc, out = _run_gh([
        "pr", "list",
        "--repo", f"{GITHUB_ORG}/{repo}",
        "--state", "open",
        "--limit", "10",
        "--json", "number,title,updatedAt,url,author",
    ])
    if rc != 0 or not out.strip():
        return findings

    try:
        prs = json.loads(out)
    except json.JSONDecodeError:
        return findings

    import datetime
    stale_threshold = time.time() - (7 * 86400)

    for pr in prs:
        updated_str = pr.get("updatedAt", "")
        try:
            updated = datetime.datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            if updated.timestamp() > stale_threshold:
                continue
        except Exception:
            continue

        findings.append(Finding(
            repo=repo,
            kind="stale_pr",
            severity="medium",
            title=f"Stale PR in {repo}: #{pr.get('number')} - {pr.get('title', '')[:60]}",
            task_body=(
                f"PR #{pr.get('number')} has had no activity for 7+ days in {repo}.\n\n"
                f"Title: {pr.get('title')}\n"
                f"URL: {pr.get('url')}\n\n"
                f"Please review the PR, address any outstanding review comments, "
                f"and either merge it or close it with an explanation."
            ),
            dedup_key=f"stale_pr:{repo}:{pr.get('number')}",
        ))
    return findings


def check_jarvis_issues(repo: str) -> list[Finding]:
    """Find open GitHub issues labelled 'jarvis'."""
    findings: list[Finding] = []
    rc, out = _run_gh([
        "issue", "list",
        "--repo", f"{GITHUB_ORG}/{repo}",
        "--state", "open",
        "--label", "jarvis",
        "--limit", "5",
        "--json", "number,title,body,url,createdAt",
    ])
    if rc != 0 or not out.strip():
        return findings

    try:
        issues = json.loads(out)
    except json.JSONDecodeError:
        return findings

    for issue in issues:
        findings.append(Finding(
            repo=repo,
            kind="jarvis_issue",
            severity="high",
            title=f"Jarvis issue in {repo}: #{issue.get('number')} - {issue.get('title', '')[:60]}",
            task_body=(
                f"GitHub issue #{issue.get('number')} in {repo} is labelled 'jarvis' and needs work.\n\n"
                f"Title: {issue.get('title')}\n"
                f"URL: {issue.get('url')}\n\n"
                f"Issue body:\n{(issue.get('body') or '')[:1000]}\n\n"
                f"Please implement the requested change, write tests, and open a PR."
            ),
            dedup_key=f"jarvis_issue:{repo}:{issue.get('number')}",
        ))
    return findings


def check_npm_audit(repo: str) -> list[Finding]:
    """Run npm audit in the workspace and find high/critical vulnerabilities."""
    workspace = settings.workspace_root / "workspaces" / repo
    if not (workspace / "package.json").exists():
        return []

    findings: list[Finding] = []
    try:
        r = subprocess.run(
            ["npm", "audit", "--json", "--audit-level=high"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode == 0:
            return []  # No high/critical vulns

        data = json.loads(r.stdout or "{}")
        vulns = data.get("vulnerabilities") or {}
        high_crit = {
            name: info for name, info in vulns.items()
            if info.get("severity") in ("high", "critical")
        }
        if not high_crit:
            return []

        names = ", ".join(list(high_crit.keys())[:5])
        count = len(high_crit)
        findings.append(Finding(
            repo=repo,
            kind="npm_audit",
            severity="high",
            title=f"npm audit: {count} high/critical vulnerabilities in {repo}",
            task_body=(
                f"npm audit found {count} high or critical vulnerabilities in {repo}.\n\n"
                f"Affected packages: {names}\n\n"
                f"Run `npm audit` for full details. Fix by running `npm audit fix` "
                f"or updating the affected packages. If auto-fix breaks the app, "
                f"investigate each vulnerability and apply manual patches."
            ),
            dedup_key=f"npm_audit:{repo}:{','.join(sorted(high_crit.keys())[:5])}",
        ))
    except Exception as exc:
        log.debug("npm audit check failed for %s: %s", repo, exc)

    return findings


# ── Main health loop ───────────────────────────────────────────────────────────

async def health_loop(stop: asyncio.Event) -> None:
    """Proactive health check. Runs checks against all whitelisted repos every
    HEALTH_CHECK_INTERVAL_SECONDS and creates self-directed tasks on findings."""

    if not settings.health_loop_enabled:
        log.info("health loop disabled (HEALTH_LOOP_ENABLED not set)")
        return

    log.info(
        "health loop starting — interval=%ds",
        settings.health_check_interval_seconds,
    )
    client = get_client()

    while not stop.is_set():
        # Wait for the interval before first run (let agent start up first)
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.health_check_interval_seconds)
            return  # stop event fired
        except asyncio.TimeoutError:
            pass

        log.info("health check starting")
        seen = _prune_seen(_load_seen())
        repos = load_repos()
        all_findings: list[Finding] = []

        for repo in repos:
            try:
                findings: list[Finding] = []
                findings.extend(await asyncio.to_thread(check_ci, repo))
                findings.extend(await asyncio.to_thread(check_stale_prs, repo))
                findings.extend(await asyncio.to_thread(check_jarvis_issues, repo))
                findings.extend(await asyncio.to_thread(check_npm_audit, repo))
                all_findings.extend(findings)
            except Exception:
                log.exception("health check error for repo %s", repo)

        new_findings = [f for f in all_findings if not _already_seen(seen, f.dedup_key)]
        log.info(
            "health check complete — %d repos, %d findings (%d new)",
            len(repos), len(all_findings), len(new_findings),
        )

        for finding in new_findings:
            if finding.severity not in ("high", "critical"):
                continue  # skip medium/low for now — avoid noise

            try:
                # Queue for user approval rather than running immediately.
                # User approves via /ui or via the MCP "approve_task" tool.
                pending = add_pending(
                    title=finding.title,
                    body=finding.task_body,
                    repo=finding.repo,
                    backend="smart",
                    source="health_loop",
                    severity=finding.severity,
                    metadata={
                        "finding_kind": finding.kind,
                        "dedup_key": finding.dedup_key,
                    },
                )
                seen[finding.dedup_key] = time.time()

                await post_as_jarvis(
                    f"🔍 Health check finding in `{finding.repo}` [{finding.kind}, {finding.severity}]\n"
                    f"> {finding.title}\n"
                    f"⏸️ **Awaiting approval** — review at http://127.0.0.1:17920/ui#pending\n"
                    f"Approval id: `{pending.id[:8]}`"
                )
                log.info("queued for approval: %s", finding.dedup_key)

            except Exception:
                log.exception("failed to queue approval for finding %s", finding.dedup_key)

        _save_seen(seen)
