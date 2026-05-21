"""Runs a single delegated task end-to-end:
clone/fetch - branch - sub-agent - self-heal - commit - push - PR - webhook - status=done.

Heartbeats every HEARTBEAT_INTERVAL_SECONDS so the reaper doesn't evict.
On any unhandled exception, sets status=blocked and posts to Discord.

Self-healing loop: after each backend run, the test suite is executed.
On failure, the backend is retried with the error context appended (up to
MAX_HEAL_ATTEMPTS times) before giving up and committing as-is."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, NamedTuple

from .agent_identity import can_run, agent_id
from .backends import get_backend
from .config import settings
from .discord_webhook import post_as_jarvis
from .jarvis_client import get_client
from .notes import emit_handover_note
from .preflight import fetch_prior_context
from .repos import is_whitelisted
from .state import bus, inject_queues, slots
from .task_terminal import (
    append_task_log,
    setup_task_terminal,
    teardown_task_terminal,
)

log = logging.getLogger("jarvis-agent.runner")

GITHUB_ORG = "Coded-Vision-Design"  # canonical org slug; GitHub is case-insensitive but `gh repo create` and the SSH path need exact match
MAX_HEAL_ATTEMPTS = 3             # max self-healing retry cycles per task

# ── Hive failover ────────────────────────────────────────────────────
# When a worker hits an infrastructure-level error (git, gh, network),
# we don't want to perma-block the task — the user's whole reason for
# running the hive is that *another* worker should pick it up. So we
# self-exclude in metadata.excluded_agents and flip status back to
# 'queued'. This repeats up to HIVE_RETRY_MAX times before we give up
# and mark the task properly blocked. Application-level failures
# (claude exited, tests failed, etc.) still go straight to blocked —
# rotating won't help when the work itself is busted.
HIVE_RETRY_MAX = 3


async def _record_deliverable_links(
    client: Any,
    task_id: int,
    repo: str,
    *,
    branch: str | None = None,
    pr_url: str | None = None,
    deploy_url: str | None = None,
    no_changes: bool | None = None,
) -> None:
    """Persist repo / PR / deploy URLs on the task so the Jarvis drawer can link them."""
    patch: dict[str, Any] = {
        "local_agent_repo": repo,
        "local_agent_repo_url": f"https://github.com/{GITHUB_ORG}/{repo}",
    }
    if branch:
        patch["local_agent_branch"] = branch
    if pr_url:
        patch["local_agent_pr_url"] = pr_url
    if deploy_url:
        patch["local_agent_deploy_url"] = deploy_url
    if no_changes is not None:
        patch["local_agent_no_changes"] = no_changes
    await client.merge_metadata(task_id, patch)


class WebDeployOutcome(NamedTuple):
    """Result of npm build + VPS rsync for a web task."""

    url: str | None = None
    failure: str | None = None  # build_failed | deploy_failed


def _deploy_ssh_cmd() -> str:
    """SSH command for rsync/deploy.

    Windows bind-mounts ``~/.ssh`` with loose permissions; OpenSSH rejects
    private keys that are group/world-readable. Copy to /tmp with mode 600.
    """
    src = Path("/root/.ssh/id_ed25519")
    cached = Path("/tmp/jarvis-deploy-ssh-key")
    key = src
    if src.is_file():
        try:
            shutil.copy2(src, cached)
            os.chmod(cached, 0o600)
            key = cached
        except OSError as exc:
            log.warning("could not cache deploy SSH key (%s), using mount", exc)
    return (
        f"ssh -F /dev/null -i {key} "
        "-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null"
    )


def error_run_payload(
    *,
    reason: str,
    message: str,
    error: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Normalise error rows for cloud Jarvis (reason/message) and legacy keys."""
    msg = message[:2000]
    err = (error if error is not None else message)[:2000]
    payload: dict[str, Any] = {
        "reason": reason[:200],
        "message": msg,
        "summary": msg,
        "error": err,
    }
    payload.update(extra)
    return payload


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


def _interpret_error(exc: Exception) -> tuple[str, str] | None:
    """Pattern-match common runner exceptions to a (diagnosis, recommended_action)
    pair. Returns None when no pattern matches — caller falls back to the raw
    traceback. The goal is to turn 'runner exception: CalledProcessError ...'
    into something the operator can act on without docker-exec-ing into the
    container.

    Captured patterns (extend as new failure modes surface):
      - gh repo create permission denied (token scope / resource owner)
      - gh PAT enterprise lifetime cap
      - gh / git not authenticated
      - git push rejected (non-fast-forward)
      - git clone permission denied (private repo not accessible)
      - docker daemon not running
      - DNS / network unreachable
      - disk full (no space left on device)
    """
    msg = str(exc)
    stderr = getattr(exc, "stderr", "") or ""
    haystack = f"{msg}\n{stderr}".lower()

    if "createrepository" in haystack and "permissions" in haystack:
        return (
            "gh repo create refused — the GITHUB_TOKEN doesn't have "
            "'Administration: write' on the Coded-Vision-Design org.",
            "Generate a fine-grained PAT with Resource owner = Coded-Vision-Design, "
            "Administration: Read and write (+ Contents / PRs / Workflows). "
            "Update GITHUB_TOKEN and JARVIS_AGENT_REPO_WRITE_TOKEN in /workspace/agent/.env, "
            "then `docker compose up -d --force-recreate jarvis-agent`.",
        )
    if "forbids access via" in haystack and "fine-grained" in haystack and "lifetime" in haystack:
        return (
            "GitHub PAT exceeds the enterprise's max lifetime (366 days).",
            "Edit the token at https://github.com/settings/personal-access-tokens, "
            "set expiry to ≤ 12 months, save, and re-paste the same value into "
            "/workspace/agent/.env.",
        )
    if "name already exists on this account" in haystack:
        return (
            "Repo name already taken on Coded-Vision-Design.",
            "Pick a different repo name in the task body, OR if you intended "
            "to reuse the existing repo, set metadata.local_agent_create_repo=false "
            "and the runner will clone the existing one instead of trying to create it.",
        )
    if "permission denied" in haystack and ("git@github" in haystack or "ssh" in haystack):
        return (
            "git clone failed with SSH permission denied — the SSH key in "
            "/root/.ssh isn't trusted by GitHub for this repo.",
            "Verify the key with `docker exec jarvis-agent ssh -T git@github.com`. "
            "Add the public key to https://github.com/settings/keys if missing.",
        )
    if "non-fast-forward" in haystack or "rejected" in haystack and "push" in haystack:
        return (
            "git push rejected — the remote branch has commits the local "
            "branch doesn't have.",
            "Run `git pull --rebase origin <branch>` from the workspace, "
            "resolve any conflicts, then push again. Usually means another agent "
            "or operator pushed to the same branch.",
        )
    if "cannot connect to the docker daemon" in haystack:
        return (
            "Docker daemon isn't reachable from inside the agent container.",
            "Ensure /var/run/docker.sock is bind-mounted in docker-compose.yml "
            "and the host docker engine is running.",
        )
    if "no space left on device" in haystack:
        return (
            "Host disk is full — the agent couldn't write to its workspace.",
            "Run the prune-disk workflow on cdv-vps-ops to reclaim docker image "
            "and buildkit cache space. Aggressive mode if the gentle prune isn't enough.",
        )
    if "could not resolve host" in haystack or "name or service not known" in haystack:
        return (
            "Network / DNS lookup failed.",
            "Check the agent container can reach the internet: "
            "`docker exec jarvis-agent curl -I https://api.github.com`. "
            "If it can't, the host's DNS or docker network may be misconfigured.",
        )
    if "push" in haystack and "non-zero exit status 128" in haystack:
        return (
            "git push exit 128 — typically the origin URL is missing "
            "credentials (gh repo create --clone leaves it as plain HTTPS), "
            "or the workspace cannot reach github.com. The runner now "
            "rewrites origin to the HTTPS-with-PAT form right after "
            "gh repo create, so this should self-heal on the next run.",
            "Re-trigger the task. If it still fails, run "
            "`docker exec jarvis-agent git -C /workspace/workspaces/<repo> "
            "remote -v` and confirm origin looks like "
            "`https://x-access-token:<token>@github.com/...`.",
        )
    if "fetch" in haystack and "non-zero exit status 128" in haystack:
        return (
            "git fetch exit 128 — stale remote auth, moved origin URL, "
            "or corrupted workspace .git. The runner now self-heals this "
            "by re-cloning fresh on the next attempt.",
            "Re-trigger the task. If it still fails, verify "
            "JARVIS_AGENT_REPO_WRITE_TOKEN is set in /workspace/agent/.env "
            "and matches a fine-grained PAT with Contents: write on the "
            "Coded-Vision-Design org. Worst case, "
            "`docker exec jarvis-agent rm -rf /workspace/workspaces/<repo>` "
            "then re-trigger.",
        )
    if "401 unauthorized" in haystack or "bad credentials" in haystack:
        return (
            "GitHub API returned 401 — the token is invalid or revoked.",
            "Re-generate the PAT at https://github.com/settings/personal-access-tokens, "
            "update /workspace/agent/.env and recreate the container.",
        )
    return None


def _is_retryable_infra_error(exc: Exception) -> bool:
    """Decide whether this exception is an *infrastructure* failure that
    another hive worker should retry, or a genuine code-task failure that
    rotating wouldn't help with.

    Retryable (rotate to another worker):
      - git / gh CalledProcessError (clone/fetch/push/remote auth, etc.)
      - DNS / network / docker-daemon issues
      - workspace permission / disk-full

    Not retryable (mark task blocked as today):
      - Backend / Claude / Codex application errors
      - Test failures, scaffold failures
      - Validation / config errors that would fail on any worker

    Conservative bias: when in doubt, prefer rotating. The cost of one
    extra rotation is small; the cost of perma-blocking a healable task
    is the user shouting at us.
    """
    if not isinstance(exc, subprocess.CalledProcessError):
        return False
    cmd = exc.cmd or []
    if not cmd:
        return False
    head = cmd[0] if isinstance(cmd[0], str) else str(cmd[0])
    head = head.rsplit("/", 1)[-1]  # tolerate "/usr/bin/git"
    if head in ("git", "gh", "ssh", "rsync", "curl", "wget", "npm", "pnpm", "node"):
        # npm/pnpm/node included because `npm ci` failures during the
        # build step are usually registry / network blips. The cap on
        # HIVE_RETRY_MAX still protects us from rotating forever.
        return True
    msg = str(exc)
    stderr = getattr(exc, "stderr", "") or ""
    haystack = f"{msg}\n{stderr}".lower()
    network_signals = (
        "could not resolve host",
        "name or service not known",
        "connection refused",
        "no route to host",
        "timed out",
        "no space left on device",
        "cannot connect to the docker daemon",
        "permission denied",
    )
    return any(sig in haystack for sig in network_signals)


def _grant_deploy_secret_access(repo: str) -> bool:
    """Add this repo to the selected list for the JARVIS_DEPLOY_* org secrets.

    These are visibility=selected secrets - only repos explicitly enrolled can
    read them. Keeps client repos isolated.

    Returns True if all four secrets were updated. Soft-fails (logs warning,
    returns False) if the secrets don't exist yet or the token lacks admin:org
    scope - in either case the deploy still works because the local agent has
    direct SSH access.
    """
    # 1. Look up the repo's numeric ID (needed by the GitHub API)
    try:
        r = _sh(["gh", "api", f"/repos/{GITHUB_ORG}/{repo}", "--jq", ".id"])
        repo_id = r.stdout.strip()
    except subprocess.CalledProcessError as exc:
        log.warning("could not fetch repo id for %s: %s", repo, exc.stderr[:200])
        return False

    if not repo_id.isdigit():
        log.warning("invalid repo id for %s: %r", repo, repo_id)
        return False

    secret_names = [
        "JARVIS_DEPLOY_SSH_KEY",
        "JARVIS_DEPLOY_HOST",
        "JARVIS_DEPLOY_BASE_PATH",
        "JARVIS_DEPLOY_DOMAIN",
    ]
    all_ok = True
    for secret_name in secret_names:
        try:
            _sh([
                "gh", "api", "--method", "PUT",
                f"/orgs/{GITHUB_ORG}/actions/secrets/{secret_name}/repositories/{repo_id}",
            ])
            log.info("enrolled %s in org secret %s", repo, secret_name)
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or "")[:200]
            if "Not Found" in err:
                log.info("org secret %s not configured yet (run setup-jarvis-deploy-secrets.sh)", secret_name)
            else:
                log.warning("failed to enrol %s in %s: %s", repo, secret_name, err)
            all_ok = False
    return all_ok


def _create_private_repo(repo: str, target: Path) -> Path:
    """Create a new private GitHub repo under GITHUB_ORG and clone it.

    Requires GITHUB_TOKEN with repo + admin:org scopes in the environment.
    After creation:
      - Adds the repo to repos.yml so future tasks can access it
      - Writes a .jarvis-prospect marker file (identifies it as a prospect site)
      - Enrols the repo in the JARVIS_DEPLOY_* org secrets (soft-fail)

    Atomicity: if any step after the local clone fails (rewrite origin,
    commit, seed-push…), the half-bootstrapped workspace is nuked before
    re-raising. Otherwise the next retry would hit `_git_clone_or_fetch`
    with a local `main` and no `origin/main`, leaving the task permanently
    stuck at "git reset --hard origin/main".
    """
    from .repos import add_to_whitelist

    log.info("creating private repo %s/%s", GITHUB_ORG, repo)
    target.parent.mkdir(parents=True, exist_ok=True)

    _sh([
        "gh", "repo", "create",
        f"{GITHUB_ORG}/{repo}",
        "--private",
        "--clone",
        "--description", "Created by Jarvis local agent",
    ], cwd=str(target.parent))

    try:
        # `gh repo create --clone` sets origin to a credential-less HTTPS
        # URL; the subsequent `git push` then dies with exit 128 because
        # the credential helper inside the container isn't wired up. Flip
        # origin to the same HTTPS-with-PAT form used by the regular
        # clone/fetch path so push works without leaning on gh's keychain.
        try:
            _sh(["git", "remote", "set-url", "origin", _origin_url(repo)], cwd=target)
        except subprocess.CalledProcessError:
            log.warning("could not rewrite origin URL after gh repo create — push may fail")

        # Set git identity for all commits in this repo
        _sh(["git", "config", "user.email", "contact@codedvisiondesign.co.uk"], cwd=target)
        _sh(["git", "config", "user.name", "Coded Vision Design"], cwd=target)

        # Seed with README + .jarvis-prospect marker
        readme = target / "README.md"
        readme.write_text(
            f"# {repo}\n\nCreated by Jarvis local agent. "
            f"Will deploy automatically to https://{repo.lower().replace('_', '-')}.codedvisiondesign.co.uk after the next successful task.\n",
            encoding="utf-8",
        )
        (target / ".jarvis-prospect").write_text(
            "# Marker file - identifies this repo as a Jarvis-built prospect site.\n"
            "# Determines which org secrets and deploy targets apply.\n"
            "# Do not delete unless converting this repo to a client/production repo.\n",
            encoding="utf-8",
        )

        _sh(["git", "add", "README.md", ".jarvis-prospect"], cwd=target)
        _sh(["git", "commit", "-m", "chore: initial repo setup by Jarvis"], cwd=target)
        _sh(["git", "push", "-u", "origin", "main"], cwd=target)
    except Exception:
        # Half-bootstrapped workspace would trap every future retry in the
        # "local main exists, origin/main missing" hole. Nuke it so the
        # next call to `_git_clone_or_fetch` starts from a clean slate
        # (the GitHub repo itself stays — `gh repo create` is idempotent
        # against "name already exists" and the empty-remote self-heal in
        # `_git_clone_or_fetch` handles the rest).
        log.warning("private repo bootstrap failed for %s — nuking workspace", repo)
        _nuke_workspace(target)
        raise

    # Register so future tasks can run against it
    add_to_whitelist(repo)

    # Enrol in org-level deploy secrets (visibility=selected) - soft-fail
    try:
        _grant_deploy_secret_access(repo)
    except Exception:
        log.exception("failed to enrol %s in deploy secrets (soft-fail)", repo)

    log.info("private repo created: https://github.com/%s/%s", GITHUB_ORG, repo)
    return target


def _github_write_token() -> str | None:
    """Token for HTTPS git/gh against Coded-Vision-Design org repos."""
    for key in ("JARVIS_AGENT_REPO_WRITE_TOKEN", "GITHUB_TOKEN"):
        tok = (os.environ.get(key) or "").strip()
        if tok:
            return tok
    # Container often has `gh auth login` but a missing compose env var.
    try:
        r = _sh(["gh", "auth", "token"], check=False)
        if r.returncode == 0 and (r.stdout or "").strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _origin_url(repo: str) -> str:
    """Prefer HTTPS with the PAT baked in over SSH.

    HTTPS works whenever the agent has a valid PAT in env; SSH needs a
    deploy key that's been added to the org. The container often only
    has one or the other; HTTPS is the safer default. The fine-grained
    JARVIS_AGENT_REPO_WRITE_TOKEN is preferred when present (it has
    repo + admin:org grants); otherwise fall back to GITHUB_TOKEN, then
    ``gh auth token``.
    """
    token = _github_write_token()
    if token:
        return f"https://x-access-token:{token}@github.com/{GITHUB_ORG}/{repo}.git"
    return f"git@github.com:{GITHUB_ORG}/{repo}.git"


def _refresh_origin_url(workspace: Path, repo: str) -> None:
    """Rewrite origin to the current PAT-backed HTTPS URL."""
    url = _origin_url(repo)
    _sh(["git", "remote", "set-url", "origin", url], cwd=workspace)


def _nuke_workspace(target: Path) -> None:
    """Remove a workspace directory (Windows bind-mount safe).

    ``shutil.rmtree(..., ignore_errors=True)`` can leave a half-deleted
    tree on NTFS bind mounts; the next ``git clone`` into the same path
    then fails and surfaces as fetch/clone exit 128. Rename-aside first
    so clone always gets a clean directory name.
    """
    if not target.exists():
        return
    trash = target.parent / f".trash-{target.name}-{int(time.time())}"
    try:
        target.rename(trash)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        return
    shutil.rmtree(trash, ignore_errors=True)


def _remote_default_branch(workspace: Path) -> str | None:
    """Resolve the remote default branch from origin/HEAD or refs/remotes/origin/*.

    Returns the bare branch name (e.g. "main"), or None when the remote
    advertises no branches at all (empty repo).
    """
    # Preferred: origin/HEAD symref tells us exactly what the remote default is.
    r = _sh(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=workspace,
        check=False,
    )
    if r.returncode == 0 and r.stdout.strip():
        # symref looks like "origin/main"
        return r.stdout.strip().split("/", 1)[-1]

    # Fallback: ask git to (re)resolve HEAD from the remote.
    r = _sh(["git", "remote", "set-head", "origin", "--auto"], cwd=workspace, check=False)
    if r.returncode == 0:
        r2 = _sh(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=workspace,
            check=False,
        )
        if r2.returncode == 0 and r2.stdout.strip():
            return r2.stdout.strip().split("/", 1)[-1]

    # Last resort: scan local remote-tracking refs and prefer main > master > first.
    r = _sh(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"],
        cwd=workspace,
        check=False,
    )
    refs = [
        line.split("/", 1)[-1]
        for line in (r.stdout or "").splitlines()
        if line and not line.endswith("/HEAD")
    ]
    if "main" in refs:
        return "main"
    if "master" in refs:
        return "master"
    return refs[0] if refs else None


def _git_clone_or_fetch(repo: str, create_if_missing: bool = False) -> Path:
    """Ensure the workspace exists and is on the remote's default branch.

    Self-heal layers (in order of cheapness):
      1. Fetch exit 128 → nuke + re-clone (stale auth / moved remote).
      2. Empty remote (zero refs after fetch) + local commits on main →
         seed the remote with `git push -u origin main`. This recovers from
         a previous `_create_private_repo` run that died after the local
         commit but before the push landed (e.g. the old token had no
         write scope on the freshly-created repo).
      3. Empty remote + nothing local → nuke + re-clone (and let the
         caller's create_if_missing path re-run repo creation if needed).
      4. Reset to origin/<default> fails → nuke + re-clone rather than
         dying with the cryptic "pathspec 'master' did not match" message.
    """
    target = settings.workspace_root / "workspaces" / repo
    target.parent.mkdir(parents=True, exist_ok=True)
    url = _origin_url(repo)

    def _fresh_clone() -> Path:
        _nuke_workspace(target)
        try:
            _sh(["git", "clone", url, str(target)])
        except subprocess.CalledProcessError as exc:
            if create_if_missing and (
                "not found" in (exc.stderr or "").lower()
                or "repository" in (exc.stderr or "").lower()
                or exc.returncode == 128
            ):
                return _create_private_repo(repo, target)
            raise
        return target

    if not (target / ".git").exists():
        return _fresh_clone()

    # Refresh origin so stale PATs baked into the URL from an earlier run
    # cannot survive a token rotation in .env.
    _refresh_origin_url(target, repo)

    def _fetch_origin() -> None:
        _sh(["git", "fetch", "--prune", "origin"], cwd=target)

    try:
        _fetch_origin()
    except subprocess.CalledProcessError as exc:
        # Exit 128 on fetch usually means stale auth embedded in origin,
        # revoked PAT, or a corrupted .git. Refresh origin from current
        # env/gh and retry once before the heavier re-clone path.
        if exc.returncode == 128:
            log.warning(
                "git fetch exit 128 in %s (stderr=%s) — refreshing origin and retrying",
                target,
                (exc.stderr or "")[:240],
            )
            _refresh_origin_url(target, repo)
            try:
                _fetch_origin()
            except subprocess.CalledProcessError as exc2:
                if exc2.returncode == 128:
                    log.warning(
                        "git fetch still exit 128 after origin refresh — re-cloning "
                        "(stderr=%s)",
                        (exc2.stderr or "")[:240],
                    )
                    return _fresh_clone()
                raise
        else:
            raise

    default_branch = _remote_default_branch(target)

    if default_branch is None:
        # Empty remote. This happens when `_create_private_repo` made the
        # repo on GitHub but the seed push never landed (e.g. previous run
        # raced a token-without-write-scope and bailed mid-flight).
        local_head = _sh(["git", "rev-parse", "--verify", "HEAD"], cwd=target, check=False)
        if local_head.returncode == 0:
            # We have local commits — push them up to seed the remote.
            current = _sh(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=target, check=False
            ).stdout.strip() or "main"
            log.warning(
                "remote %s has no branches — seeding with local %s", repo, current
            )
            try:
                _sh(["git", "push", "-u", "origin", current], cwd=target)
                return target
            except subprocess.CalledProcessError as exc:
                log.warning(
                    "seed push to %s failed (%s) — re-cloning from scratch",
                    repo, (exc.stderr or "")[:240],
                )
                return _fresh_clone()
        # No local commits either: nuke and let the create-if-missing
        # branch (if enabled) re-bootstrap the repo cleanly.
        log.warning("remote %s is empty and local has no commits — re-cloning", repo)
        return _fresh_clone()

    # Hard-reset to the resolved remote default branch so every task
    # starts from a known-clean state.
    try:
        _sh(["git", "checkout", default_branch], cwd=target)
        _sh(["git", "reset", "--hard", f"origin/{default_branch}"], cwd=target)
    except subprocess.CalledProcessError as exc:
        log.warning(
            "could not align %s with origin/%s (%s) — re-cloning from scratch",
            target, default_branch, (exc.stderr or "")[:240],
        )
        return _fresh_clone()

    return target


def _make_branch(workspace: Path, slug: str) -> str:
    name = f"jarvis/{slug}-{int(time.time())}"
    _sh(["git", "checkout", "-b", name], cwd=workspace)
    return name


# Build outputs alone must not count as "agent shipped code" for stack gates.
_ARTIFACT_PATH_PREFIXES = (
    ".next/",
    "node_modules/",
    "out/",
    "dist/",
    "build/",
    ".turbo/",
)


def _has_uncommitted_changes(workspace: Path) -> bool:
    """True when there are meaningful source changes (not just out/.next noise)."""
    r = _sh(["git", "status", "--porcelain", "-u"], cwd=workspace)
    for line in r.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if path.endswith("tsconfig.tsbuildinfo"):
            continue
        if any(
            path.startswith(prefix) or path == prefix.rstrip("/")
            for prefix in _ARTIFACT_PATH_PREFIXES
        ):
            continue
        return True
    return False


def _restore_next_scaffold_from_origin(workspace: Path) -> str | None:
    """Restore Next.js scaffold files from the newest origin/jarvis/* branch.

    Handles branches whose HEAD is only CLAUDE.md while an older jarvis branch
    still has the full static-export tree.
    """
    if (workspace / "package.json").exists():
        return None
    try:
        _sh(["git", "fetch", "origin"], cwd=workspace)
    except subprocess.CalledProcessError:
        log.warning("git fetch failed before scaffold restore")
    try:
        r = _sh(
            [
                "git",
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)",
                "refs/remotes/origin/jarvis/",
            ],
            cwd=workspace,
        )
    except subprocess.CalledProcessError:
        return None
    branches = [b.strip() for b in r.stdout.splitlines() if b.strip()]
    restore_paths = [
        "package.json",
        "package-lock.json",
        "next.config.mjs",
        "next.config.ts",
        "next.config.js",
        "postcss.config.mjs",
        "tsconfig.json",
        ".gitignore",
        "app",
        "public",
        "tests",
    ]
    for ref in branches:
        remote = ref if ref.startswith("origin/") else f"origin/{ref}"
        branch = remote.removeprefix("origin/")
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{remote}:package.json"],
            cwd=str(workspace),
            capture_output=True,
        )
        if probe.returncode != 0:
            continue
        try:
            _sh(["git", "checkout", remote, "--", *restore_paths], cwd=workspace)
            log.info("restored Next scaffold from %s into %s", remote, workspace)
            return branch  # jarvis/... without origin/ prefix
        except subprocess.CalledProcessError:
            continue
    return None


def _head_commit_shas(workspace: Path, limit: int = 8) -> list[str]:
    """Recent commit SHAs on the current branch — fed into handover artefacts."""
    try:
        r = subprocess.run(
            ["git", "log", "-n", str(limit), "--format=%H"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            return []
        return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []


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


async def _run_test_cmd(cmd: list[str], workspace: Path, timeout: int = 180) -> tuple[bool, str]:
    """Run a single test command and return (passed, output_tail)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return False, f"test run timed out after {timeout}s"
        output = stdout.decode(errors="replace")
        tail = output[-4000:] if len(output) > 4000 else output
        return proc.returncode == 0, tail
    except FileNotFoundError:
        return True, f"test runner not available ({cmd[0]}) - skipping"
    except Exception as exc:
        return False, f"test runner failed: {exc}"


def _is_big_change(task_body: str) -> bool:
    """Return True if the task implies a large refactor/migration that needs regression tests."""
    lower = task_body.lower()
    triggers = [
        "refactor", "migrate", "migration", "redesign", "rewrite",
        "architecture", "system design", "overhaul", "rename ",
        "move ", "restructure", "major version",
    ]
    return any(t in lower for t in triggers)


async def _detect_and_run_tests(workspace: Path, *, run_regression: bool = False) -> tuple[bool, str]:
    """Layered test execution: smoke first, then full suite, then regression if requested.

    Order:
      1. smoke tests (fast fail - < 30s)
      2. full suite (unit + non-mutating integration + DB-tx + mocked API)
      3. regression tests (only if run_regression=True)

    Returns (passed, output_tail). If any layer fails, stops and returns that failure.
    """
    pkg_json = workspace / "package.json"
    has_npm = pkg_json.exists()
    has_phpunit = (workspace / "vendor" / "bin" / "phpunit").exists()
    has_pytest = (
        (workspace / "pytest.ini").exists()
        or (workspace / "pyproject.toml").exists()
        or (workspace / "setup.cfg").exists()
    )

    layers: list[tuple[str, list[str], int]] = []  # (name, cmd, timeout_s)

    # ── Smoke layer (< 30 s) ──────────────────────────────────────────────
    if has_npm:
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            if "test:smoke" in scripts:
                layers.append(("smoke", ["npm", "run", "test:smoke"], 60))
            elif "vitest" in str(scripts.get("test", "")):
                layers.append(("smoke", ["npm", "test", "--", "--run", "smoke"], 60))
        except Exception:
            pass
    if has_pytest:
        layers.append(("smoke", ["python", "-m", "pytest", "-x", "-q", "tests/smoke.py", "tests/smoke"], 60))

    # ── Full suite (unit + integration + DB-tx + API) ─────────────────────
    if has_npm:
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                test_script = scripts["test"]
                if "vitest" in test_script:
                    layers.append(("unit+integration", ["npm", "test", "--", "--run", "--reporter=verbose"], 300))
                elif "jest" in test_script:
                    layers.append(("unit+integration", ["npm", "test", "--", "--watchAll=false", "--ci"], 300))
                else:
                    layers.append(("unit+integration", ["npm", "test"], 300))
        except Exception:
            pass
    if has_phpunit:
        layers.append(("unit+integration", ["./vendor/bin/phpunit", "--no-coverage", "--colors=never"], 300))
    if has_pytest and not has_npm:  # avoid double-run if Vitest already covers it
        layers.append(("unit+integration", ["python", "-m", "pytest", "-x", "--tb=short", "-q"], 300))

    # ── Regression suite (only for big changes) ────────────────────────────
    if run_regression:
        if has_npm:
            layers.append(("regression", ["npm", "test", "--", "--run", "regression"], 600))
        if has_pytest:
            layers.append(("regression", ["python", "-m", "pytest", "-x", "tests/regression"], 600))
        if has_phpunit:
            layers.append(("regression", ["./vendor/bin/phpunit", "--testsuite=Regression"], 600))

    if not layers:
        return True, "no test runner detected - skipping self-heal check"

    combined_output: list[str] = []
    for layer_name, cmd, timeout in layers:
        passed, tail = await _run_test_cmd(cmd, workspace, timeout=timeout)
        combined_output.append(f"=== {layer_name} ===\n{tail}\n")
        if not passed:
            # Stop at first failing layer - faster feedback
            full = "\n".join(combined_output)
            return False, full[-4000:]

    full = "\n".join(combined_output)
    return True, full[-4000:]


def _ensure_claude_md(workspace: Path) -> bool:
    """Drop CLAUDE.md into the project root if missing.

    Returns True if the file was created/updated, False if it already existed.
    This is how the website rules (responsive, sticky header, no h-scroll, OWASP, TDD)
    reach every agent that runs in this workspace.
    """
    template = settings.workspace_root / "agent" / "templates" / "CLAUDE.md"
    target = workspace / "CLAUDE.md"
    if target.exists():
        return False
    if not template.exists():
        log.warning("CLAUDE.md template missing at %s", template)
        return False
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    log.info("seeded CLAUDE.md in %s", workspace)
    return True


def _detect_build_output(workspace: Path) -> Path | None:
    """Find the build output directory for a web project."""
    for candidate in ("dist", "build", "out", ".output/public", "public"):
        path = workspace / candidate
        if path.is_dir() and any(path.iterdir()):
            # Skip if it's just the React public/ source folder (not a build output)
            if candidate == "public" and not (path / "index.html").exists():
                continue
            return path
    return None


def _looks_like_web_project(workspace: Path) -> bool:
    """Heuristic: does this workspace look like a website / web app?"""
    pkg = workspace / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
            web_signals = {"react", "vue", "next", "nuxt", "astro", "vite", "svelte", "@angular/core"}
            if any(d in deps for d in web_signals):
                return True
            scripts = data.get("scripts") or {}
            if "build" in scripts:
                return True
        except Exception:
            pass
    # PHP site with index.php
    if (workspace / "index.php").exists() or (workspace / "public" / "index.php").exists():
        return True
    return False


def _build_web_project(workspace: Path) -> Path | None:
    """Run `npm run build` (or equivalent) to produce a dist directory."""
    pkg = workspace / "package.json"
    if not pkg.exists():
        return _detect_build_output(workspace)

    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
        scripts = data.get("scripts") or {}
        if "build" not in scripts:
            return _detect_build_output(workspace)
    except Exception:
        return _detect_build_output(workspace)

    log.info("running npm ci && npm run build in %s", workspace)
    try:
        _sh(["npm", "ci"], cwd=workspace)
        _sh(["npm", "run", "build"], cwd=workspace)
    except subprocess.CalledProcessError as exc:
        log.warning("build failed: %s", exc.stderr[-500:] if exc.stderr else exc)
        return None

    return _detect_build_output(workspace)


async def _build_and_deploy_web_task(
    client: Any,
    task_id: int,
    workspace: Path,
    repo: str,
    branch: str,
    metadata: dict[str, Any],
) -> WebDeployOutcome:
    """Run production build + VPS rsync."""
    should_deploy = (
        settings.deploy_enabled
        and metadata.get("local_agent_deploy", True) is not False
        and await asyncio.to_thread(_looks_like_web_project, workspace)
    )
    if not should_deploy:
        return WebDeployOutcome()

    await client.append_run(task_id, "jarvis_note", {
        "message": "web project detected - building and deploying to VPS",
    })
    dist_dir = await asyncio.to_thread(_build_web_project, workspace)
    if dist_dir is None:
        await client.append_run(
            task_id,
            "error",
            error_run_payload(
                reason="build_failed",
                message=(
                    "npm build failed or produced no dist/out folder — "
                    "check package.json scripts and next.config export settings"
                ),
            ),
        )
        return WebDeployOutcome(failure="build_failed")

    deploy_url = await asyncio.to_thread(_deploy_to_vps, str(repo), dist_dir)
    if not deploy_url:
        await client.append_run(
            task_id,
            "error",
            error_run_payload(
                reason="deploy_failed",
                message=(
                    "deploy to VPS failed (rsync/SSH). On Windows hosts, ensure "
                    "/root/.ssh/id_ed25519 is readable and DEPLOY_HOST is reachable"
                ),
            ),
        )
        return WebDeployOutcome(failure="deploy_failed")

    await asyncio.to_thread(_update_readme_with_url, workspace, deploy_url)
    try:
        if await asyncio.to_thread(_has_uncommitted_changes, workspace):
            await asyncio.to_thread(_sh, ["git", "add", "README.md"], cwd=workspace)
            await asyncio.to_thread(
                _sh,
                [
                    "git",
                    "-c", "user.email=contact@codedvisiondesign.co.uk",
                    "-c", "user.name=Coded Vision Design",
                    "commit",
                    "-m", f"docs: add live site URL ({deploy_url})",
                ],
                cwd=workspace,
            )
            await asyncio.to_thread(_sh, ["git", "push", "origin", branch], cwd=workspace)
    except Exception:
        log.exception("failed to commit README deploy URL")

    await client.append_run(task_id, "jarvis_note", {
        "message": f"deployed to {deploy_url}",
    })
    return WebDeployOutcome(url=deploy_url)


def _deploy_to_vps(repo: str, dist_dir: Path) -> str | None:
    """Rsync the built dist directory to the VPS subdomain folder.

    Returns the deploy URL on success, None on failure.
    Atomic: rsyncs to a .new directory, then moves into place to avoid serving partial files.
    """
    if not settings.deploy_enabled:
        log.info("deploy disabled - skipping")
        return None

    slug = repo.lower().replace("_", "-")
    remote_base = f"{settings.deploy_host}:{settings.deploy_base_path}"
    remote_target = f"{remote_base}/{slug}"
    remote_staging = f"{remote_base}/.{slug}.staging"
    url = f"https://{slug}.{settings.deploy_domain}"

    ssh_cmd = _deploy_ssh_cmd()

    try:
        # Stage to a sibling directory, then atomically swap
        _sh(["ssh"] + ssh_cmd.split()[1:] + [settings.deploy_host,
             f"rm -rf {settings.deploy_base_path}/.{slug}.staging "
             f"&& mkdir -p {settings.deploy_base_path}/.{slug}.staging"])

        # Rsync dist contents into staging (note trailing slash)
        _sh([
            "rsync", "-az", "--delete",
            "-e", ssh_cmd,
            f"{dist_dir}/",
            remote_staging,
        ])

        # Atomic swap: move current out, move staging in, clean up
        _sh(["ssh"] + ssh_cmd.split()[1:] + [settings.deploy_host,
             f"rm -rf {settings.deploy_base_path}/.{slug}.old "
             f"&& if [ -d {settings.deploy_base_path}/{slug} ]; then "
             f"   mv {settings.deploy_base_path}/{slug} {settings.deploy_base_path}/.{slug}.old; "
             f"fi "
             f"&& mv {settings.deploy_base_path}/.{slug}.staging {settings.deploy_base_path}/{slug} "
             f"&& rm -rf {settings.deploy_base_path}/.{slug}.old"])

        log.info("deployed %s to %s", repo, url)
        return url

    except subprocess.CalledProcessError as exc:
        log.warning("deploy failed for %s: %s", repo, exc.stderr[-500:] if exc.stderr else exc)
        return None
    except Exception:
        log.exception("deploy crashed for %s", repo)
        return None


def _update_readme_with_url(workspace: Path, url: str) -> None:
    """Add or update a 'Live site' badge / line in the project README."""
    readme = workspace / "README.md"
    marker = "<!-- jarvis-deploy-url -->"
    deploy_block = (
        f"\n{marker}\n"
        f"**Live site:** [{url}]({url})\n"
        f"{marker}\n"
    )
    if not readme.exists():
        readme.write_text(
            f"# {workspace.name}\n{deploy_block}\n",
            encoding="utf-8",
        )
        return

    content = readme.read_text(encoding="utf-8")
    if marker in content:
        # Replace existing block
        import re
        pattern = rf"{re.escape(marker)}.*?{re.escape(marker)}\n?"
        content = re.sub(pattern, deploy_block.strip() + "\n", content, flags=re.DOTALL)
    else:
        # Insert after the H1 title if present, otherwise prepend
        lines = content.splitlines(keepends=True)
        if lines and lines[0].startswith("# "):
            lines.insert(1, deploy_block)
            content = "".join(lines)
        else:
            content = deploy_block + content
    readme.write_text(content, encoding="utf-8")


async def _heartbeat_loop(task_id: int, stop: asyncio.Event) -> None:
    interval = settings.heartbeat_active_interval_seconds
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
    backend_name = metadata.get("local_agent_backend") or "smart"
    repo = metadata.get("local_agent_repo") or task.get("related_entity")
    body = task.get("body") or task.get("title", "")
    title_slug = _slug(task.get("title") or "task")
    client = get_client()

    # ── Multi-host gate: skip tasks targeted at other agents ──────────
    ok, reason = can_run(metadata)
    if not ok:
        await client.append_run(task_id, "jarvis_note", {
            "message": f"agent {agent_id()} skipping task: {reason}",
        })
        # Push back to queued so a capable agent can claim it
        await client.merge_metadata(task_id, {
            "claim_hold_until": time.time() + 30,
        })
        await client.set_status(task_id, "queued")
        return

    # Phase G1 — bypass the whitelist when the task explicitly opts into
    # scaffold mode (metadata.local_agent_create_repo=True). In that
    # case _git_clone_or_fetch → _create_private_repo runs `gh repo
    # create` AND appends the repo to repos.yml via add_to_whitelist
    # before the backend ever sees it, so the whitelist check is
    # redundant (and would block the very flow it should enable).
    #
    # Heuristic fallback: detect when the task body implies the operator
    # wants a new repo scaffolded. We split signals into two tiers:
    #
    #   STRONG hints — explicit "create the repo if missing" phrasings.
    #   Any one of these is enough on its own.
    #
    #   SCAFFOLD hints — common build-and-ship phrasings ("build a
    #   website", "scaffold", "deploy to vercel", "open a PR"). On their
    #   own a SCAFFOLD hint isn't enough — but combined with `repo`
    #   missing from the whitelist (no pre-existing repo with that
    #   name), it's the cleanest possible signal that the operator
    #   expects scaffold mode. Two SCAFFOLD hints together also trigger.
    #
    # False positives only matter if the repo name collides with something
    # real on the CVD org — `gh repo create` errors out cleanly in that
    # case, and the runner falls back to clone-existing.
    scaffold_requested = bool(metadata.get("local_agent_create_repo"))
    if not scaffold_requested and isinstance(body, str):
        body_lower = body.lower()
        strong_hints = (
            "if the repo doesn't exist yet, create it",
            "if the repo doesn't exist, create it",
            "create the repo if it doesn't exist",
            "scaffold a new",
            "scaffold the repo",
            "private is fine",
            "private repo is fine",
        )
        scaffold_hints = (
            "build a polished",
            "build a one-page",
            "build a portfolio",
            "build a demo",
            "build a landing page",
            "build a website",
            "build the website",
            "scaffold",
            "deploy to vercel",
            "open a pr against main",
            "open a pr when done",
            "deploy-ready",
        )
        repo_unknown = not (repo and is_whitelisted(str(repo)))
        if any(h in body_lower for h in strong_hints):
            scaffold_requested = True
        else:
            hits = sum(1 for h in scaffold_hints if h in body_lower)
            if hits >= 2 or (hits >= 1 and repo_unknown):
                scaffold_requested = True
        if scaffold_requested:
            metadata["local_agent_create_repo"] = True

    if not repo or (not is_whitelisted(str(repo)) and not scaffold_requested):
        await client.append_run(
            task_id,
            "error",
            {"message": f"repo {repo!r} not whitelisted; refusing to run"},
        )
        await client.set_status(task_id, "blocked")
        await post_as_jarvis(
            f"❌ Task #{task_id} blocked: repo `{repo}` isn't whitelisted in `C:\\Jarvis\\agent\\repos.yml`."
            f" If this should auto-scaffold, set `metadata.local_agent_create_repo=true` on the task."
        )
        return

    sem = slots.acquire(backend_name)
    if sem.locked():
        # Another job of this backend is in flight — put us back in the queue.
        await client.append_run(
            task_id, "jarvis_note",
            {"message": f"{backend_name} slot busy; requeuing"},
        )
        await client.merge_metadata(task_id, {
            "claim_hold_until": time.time() + 45,
        })
        await client.set_status(task_id, "queued")
        return

    # Register with StreamBus so the live UI can pick it up immediately.
    bus.task_started(task_id, {
        "backend": backend_name,
        "repo": repo,
        "title": task.get("title", ""),
    })
    inject_queues[task_id] = asyncio.Queue(maxsize=10)

    try:
        term_meta = setup_task_terminal(task_id)
        await client.merge_metadata(task_id, term_meta)
    except Exception:
        log.exception("task %s terminal setup failed", task_id)

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
            create_repo = bool(metadata.get("local_agent_create_repo", False))
            workspace = await asyncio.to_thread(
                _git_clone_or_fetch, str(repo), create_repo
            )
            branch = await asyncio.to_thread(_make_branch, workspace, title_slug)
            await _record_deliverable_links(client, task_id, str(repo), branch=branch)
            await client.append_run(task_id, "jarvis_note", {
                "message": f"working in {workspace} on branch {branch}",
            })

            # Seed CLAUDE.md (project rules) so every agent reads the same standards
            if await asyncio.to_thread(_ensure_claude_md, workspace):
                await client.append_run(task_id, "jarvis_note", {
                    "message": "seeded CLAUDE.md with mandatory website rules",
                })

            # 2. Run backend
            backend = get_backend(backend_name)

            # Phase 19: pre-work retrieval. Pull prior handover notes
            # for this entity + pattern matches across the corpus and
            # prepend them so Jarvis sees lessons from prior work
            # before its first move. Best-effort — if the calls fail
            # the runner keeps going with an empty block.
            prior_context_block = ""
            try:
                prior_context_block = await fetch_prior_context(client, task)
                if prior_context_block:
                    await client.append_run(task_id, "jarvis_note", {
                        "message": f"prepended prior context ({len(prior_context_block)} chars from task_notes)",
                    })
            except Exception as _exc:
                log.warning("prior context fetch failed: %s", _exc)

            # Prepend coding standards to every task so agents use correct versions
            standards_path = settings.workspace_root / "agent" / "prompts" / "CODING_STANDARDS.md"
            task_with_standards = body
            if standards_path.exists():
                standards = standards_path.read_text(encoding="utf-8")
                task_with_standards = (
                    f"<coding_standards>\n{standards}\n</coding_standards>\n\n"
                    f"<task>\n{body}\n</task>"
                )
            if prior_context_block:
                task_with_standards = (
                    f"<prior_context>\n{prior_context_block}\n</prior_context>\n\n"
                    f"{task_with_standards}"
                )

            from .stack_policy import (
                contract_from_metadata,
                format_stack_failure_message,
                format_stack_prompt_block,
                pre_scaffold_workspace,
                should_validate_stack,
                validate_workspace_against_contract,
            )
            stack_contract = contract_from_metadata(metadata)
            if stack_contract:
                task_with_standards = (
                    f"{format_stack_prompt_block(stack_contract)}{task_with_standards}"
                )
                await client.append_run(task_id, "jarvis_note", {
                    "message": (
                        f"stack contract: {stack_contract.get('profile_id')} "
                        f"({stack_contract.get('label', '')})"
                    ),
                })
                # When the workspace is empty (fresh repo) and the
                # profile requires specific files, drop a minimal
                # skeleton so Claude has a starting point and the
                # validator can find the required-paths even if Claude
                # underdelivers. No-op for inherit-repo / ops-bash /
                # python-fastapi and for workspaces that already have a
                # package.json.
                scaffold_files = await asyncio.to_thread(
                    pre_scaffold_workspace,
                    workspace,
                    stack_contract,
                    str(repo),
                )
                if scaffold_files:
                    await client.append_run(task_id, "jarvis_note", {
                        "message": (
                            f"pre-scaffolded {len(scaffold_files)} files for "
                            f"{stack_contract.get('profile_id')}: "
                            f"{', '.join(scaffold_files[:6])}"
                            f"{'…' if len(scaffold_files) > 6 else ''}"
                        ),
                    })

            # Phase 19 — track tool calls + errors for the auto-note
            # context. Capped to recent N so the note prompt stays small.
            recent_tools: list[str] = []
            recent_errors: list[str] = []
            tool_error_counts: dict[str, int] = {}

            async def log_cb(kind: str, payload: dict) -> None:
                await client.append_run(task_id, kind, payload)
                await bus.put_event(task_id, kind, payload)
                if kind == "terminal_line":
                    line = payload.get("line")
                    if isinstance(line, str):
                        append_task_log(task_id, line)
                # Phase 19 — track for the note context + fire the
                # tool_error trigger on the 2nd consecutive failure
                # of the same tool (one-off transients don't qualify).
                if kind == "tool_call":
                    tname = payload.get("name") or ""
                    recent_tools.append(tname)
                    if len(recent_tools) > 12:
                        del recent_tools[: len(recent_tools) - 12]
                elif kind == "tool_result":
                    # Reset the counter on success — only consecutive
                    # failures of the SAME tool trigger a note.
                    tname = payload.get("name") or ""
                    if tname and tool_error_counts.get(tname):
                        tool_error_counts[tname] = 0
                elif kind == "error":
                    msg = payload.get("message") or payload.get("error") or ""
                    if msg:
                        recent_errors.append(str(msg)[:300])
                        if len(recent_errors) > 6:
                            del recent_errors[: len(recent_errors) - 6]
                    # Did this error come from a recently-attempted tool?
                    tname = (recent_tools or [""])[-1]
                    if tname:
                        tool_error_counts[tname] = tool_error_counts.get(tname, 0) + 1
                        if tool_error_counts[tname] == 2:
                            # Fire-and-forget the note so the runner
                            # never blocks on note generation.
                            asyncio.create_task(emit_handover_note(
                                client, task, "tool_error", backend_name,
                                context={
                                    "recent_tools": list(recent_tools),
                                    "recent_errors": list(recent_errors),
                                },
                                artefacts={"tool": tname, "consecutive_failures": 2},
                            ))

            async def write_handover(
                trigger: str, artefacts: dict[str, Any] | None = None,
            ) -> None:
                """Inline shortcut so each trigger site stays one-line."""
                await emit_handover_note(
                    client, task, trigger, backend_name,
                    context={
                        "recent_tools": list(recent_tools),
                        "recent_errors": list(recent_errors),
                        "user_steer": metadata.get("last_user_steer", ""),
                    },
                    artefacts=artefacts or {},
                )

            result = await backend.run(task_with_standards, workspace, log_cb,
                                       inject_queue=inject_queues.get(task_id))

            # 3. Self-healing loop - run tests; retry backend on failure
            # Detect refactor / migrate / big-change tasks for regression coverage
            needs_regression = _is_big_change(body)
            if needs_regression:
                await client.append_run(task_id, "jarvis_note", {
                    "message": "big-change task detected - regression tests will run after main suite",
                })

            if result.ok:
                for heal_attempt in range(MAX_HEAL_ATTEMPTS):
                    tests_passed, test_output = await _detect_and_run_tests(
                        workspace, run_regression=needs_regression,
                    )

                    if tests_passed:
                        if heal_attempt > 0:
                            await log_cb("jarvis_note", {
                                "message": f"tests green after {heal_attempt} healing attempt(s)",
                            })
                        break  # all good - proceed to commit

                    is_last = heal_attempt == MAX_HEAL_ATTEMPTS - 1
                    await log_cb("jarvis_note", {
                        "message": (
                            f"tests failed (heal attempt {heal_attempt + 1}/{MAX_HEAL_ATTEMPTS})"
                            + (" - giving up" if is_last else " - retrying...")
                        ),
                        "test_output_tail": test_output[-500:],
                    })

                    if is_last:
                        break  # commit whatever we have; tests still fail

                    heal_prompt = (
                        f"{task_with_standards}\n\n"
                        f"<heal_context attempt=\"{heal_attempt + 1}\">\n"
                        f"Your previous implementation made the test suite fail. "
                        f"Read the failure output below and fix ALL failing tests "
                        f"without breaking passing ones. Do not change test files "
                        f"unless the test itself is clearly wrong.\n\n"
                        f"--- TEST FAILURES ---\n{test_output}\n"
                        f"--- END TEST FAILURES ---\n"
                        f"</heal_context>"
                    )
                    result = await backend.run(
                        heal_prompt, workspace, log_cb,
                        inject_queue=inject_queues.get(task_id),
                    )
                    if not result.ok:
                        break  # backend itself failed - stop healing

            # 4. Commit / push / PR (if there are changes)
            if not result.ok:
                await client.append_run(
                    task_id,
                    "error",
                    error_run_payload(
                        reason="backend_failed",
                        message=result.summary or "backend run failed",
                        error=result.error or result.summary or "",
                    ),
                )
                # Phase 19 trigger — write a structured blocked-note
                # before flipping status so the next engineer (or the
                # next Jarvis run) picks up knowing what stalled.
                await write_handover("blocked", artefacts={
                    "summary": result.summary[:400],
                    "error": (result.error or "")[:400],
                })
                await client.set_status(task_id, "blocked")
                await post_as_jarvis(
                    f"⚠️ Task #{task_id} ({backend_name}, {repo}) failed: {result.summary[:300]}"
                )
                return

            changed = await asyncio.to_thread(_has_uncommitted_changes, workspace)
            if stack_contract and should_validate_stack(task, metadata):
                if stack_contract.get("profile_id") == "next-static-export":
                    restored = await asyncio.to_thread(
                        _restore_next_scaffold_from_origin, workspace,
                    )
                    if restored:
                        await client.append_run(task_id, "jarvis_note", {
                            "message": (
                                f"restored Next.js scaffold from origin/{restored} "
                                "(branch HEAD was missing package.json / app/)"
                            ),
                        })
            if changed and stack_contract and should_validate_stack(task, metadata):
                stack_ok, stack_failures = await asyncio.to_thread(
                    validate_workspace_against_contract, workspace, stack_contract,
                )
                if not stack_ok:
                    profile = stack_contract.get("profile_id")
                    await client.append_run(
                        task_id,
                        "error",
                        error_run_payload(
                            reason="stack_mismatch",
                            message=format_stack_failure_message(
                                stack_failures, str(profile) if profile else None,
                            ),
                            error="; ".join(stack_failures)[:2000],
                            failures=stack_failures,
                            profile_id=profile,
                        ),
                    )
                    await write_handover("blocked", artefacts={
                        "stack_profile": stack_contract.get("profile_id"),
                        "validation_errors": stack_failures,
                    })
                    await client.set_status(task_id, "blocked")
                    await post_as_jarvis(
                        f"⚠️ Task #{task_id} blocked — stack mismatch "
                        f"({stack_contract.get('profile_id')}): "
                        f"{stack_failures[0][:200]}"
                    )
                    return

            if not changed:
                from .task_evidence import is_build_class_task, no_changes_should_block

                async def _finish_prefilled_deploy(outcome: WebDeployOutcome) -> bool:
                    """Complete a build-class task from existing scaffold. Return True if done."""
                    if not outcome.url:
                        return False
                    deploy_url = outcome.url
                    title = (task.get("title") or "jarvis: delegated change").split("\n")[0]
                    pr_body = (
                        f"Delegated by Jarvis (task #{task_id}, backend={backend_name}).\n\n"
                        f"**Request:**\n\n{body[:4000]}\n\n"
                        f"**Note:** Site built from existing scaffold on branch `{branch}`.\n"
                    )
                    pr_url = await asyncio.to_thread(_open_pr, workspace, title, pr_body)
                    await _record_deliverable_links(
                        client,
                        task_id,
                        str(repo),
                        branch=branch,
                        pr_url=pr_url or None,
                        deploy_url=deploy_url,
                        no_changes=False,
                    )
                    elapsed = int(time.time() - started)
                    await write_handover("done", artefacts={
                        "pr_url": pr_url,
                        "branch": branch,
                        "deploy_url": deploy_url,
                        "commits": await asyncio.to_thread(_head_commit_shas, workspace),
                        "time_spent_seconds": elapsed,
                        "spent_pence": result.spent_pence,
                        "prefilled_scaffold": True,
                    })
                    await client.set_status(
                        task_id,
                        "done",
                        spent_pence=result.spent_pence,
                        spent_tokens=result.spent_tokens,
                    )
                    mins, secs = divmod(elapsed, 60)
                    elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"
                    msg = (
                        f"✅ **Task #{task_id}** ({backend_name}, `{repo}`)\n"
                        f"Branch `{branch}` · scaffold deploy · {elapsed_str}\n"
                        f"🌐 Live: <{deploy_url}>"
                    )
                    if pr_url:
                        msg += f" · PR <{pr_url}>"
                    await post_as_jarvis(msg)
                    return True

                async def _block_web_failure(
                    failure: str,
                    *,
                    prefilled: bool,
                ) -> None:
                    messages = {
                        "build_failed": (
                            "Build failed or produced no output folder (dist/out). "
                            "Fix package.json / next.config, then retry."
                        ),
                        "deploy_failed": (
                            "Deploy to VPS failed (rsync/SSH). Check agent logs and "
                            "DEPLOY_HOST / SSH key on the workstation."
                        ),
                    }
                    msg = messages.get(failure, "Web build or deploy failed.")
                    elapsed = int(time.time() - started)
                    await write_handover("blocked", artefacts={
                        "time_spent_seconds": elapsed,
                        "no_changes": True,
                        "spent_pence": result.spent_pence,
                        "block_reason": failure,
                        "prefilled_scaffold": prefilled,
                    })
                    await client.set_status(task_id, "blocked")
                    await post_as_jarvis(
                        f"⚠️ Task #{task_id} ({backend_name}, `{repo}`) blocked — "
                        f"{failure.replace('_', ' ')}. _{elapsed}s_"
                    )

                # Pre-scaffolded repo: agent may no-op while workspace already
                # satisfies the stack contract — still build + deploy.
                if is_build_class_task(task):
                    stack_ok = True
                    stack_failures: list[str] = []
                    if stack_contract and should_validate_stack(task, metadata):
                        if stack_contract.get("profile_id") == "next-static-export":
                            restored = await asyncio.to_thread(
                                _restore_next_scaffold_from_origin, workspace,
                            )
                            if restored:
                                await client.append_run(task_id, "jarvis_note", {
                                    "message": (
                                        f"restored Next.js scaffold from origin/{restored} "
                                        "(branch HEAD was missing package.json / app/)"
                                    ),
                                })
                        stack_ok, stack_failures = await asyncio.to_thread(
                            validate_workspace_against_contract,
                            workspace,
                            stack_contract,
                        )
                        if not stack_ok:
                            profile = stack_contract.get("profile_id")
                            await client.append_run(
                                task_id,
                                "error",
                                error_run_payload(
                                    reason="stack_mismatch",
                                    message=format_stack_failure_message(
                                        stack_failures,
                                        str(profile) if profile else None,
                                    ),
                                    error="; ".join(stack_failures)[:2000],
                                    failures=stack_failures,
                                    profile_id=profile,
                                ),
                            )
                            await write_handover("blocked", artefacts={
                                "stack_profile": stack_contract.get("profile_id"),
                                "validation_errors": stack_failures,
                            })
                            await client.set_status(task_id, "blocked")
                            await post_as_jarvis(
                                f"⚠️ Task #{task_id} blocked — stack mismatch "
                                f"({stack_contract.get('profile_id')}): "
                                f"{stack_failures[0][:200]}"
                            )
                            return

                    if stack_ok and await asyncio.to_thread(
                        _looks_like_web_project, workspace
                    ):
                        await client.append_run(task_id, "jarvis_note", {
                            "message": (
                                "workspace already matches stack contract — "
                                "building and deploying without new agent edits"
                            ),
                        })
                        outcome = await _build_and_deploy_web_task(
                            client, task_id, workspace, str(repo), branch, metadata,
                        )
                        if await _finish_prefilled_deploy(outcome):
                            return
                        if outcome.failure:
                            await _block_web_failure(
                                outcome.failure, prefilled=True,
                            )
                            return

                should_block, block_reason = no_changes_should_block(
                    task, backend_name=backend_name,
                )
                await _record_deliverable_links(
                    client, task_id, str(repo), branch=branch, no_changes=True,
                )
                elapsed = int(time.time() - started)
                if should_block:
                    msg = (
                        "Sub-agent finished with no file changes on a task that "
                        "requires shipped code (PR, commits, or deploy). "
                        "Re-steer the task body or set metadata.allow_no_changes "
                        "for doc-only work."
                    )
                    if block_reason == "codex_advisory_no_files":
                        msg = (
                            "Codex ran in advisory mode and did not write files. "
                            "Use backend claude or qwen for implementation tasks."
                        )
                    await client.append_run(
                        task_id,
                        "error",
                        error_run_payload(
                            reason=block_reason,
                            message=msg,
                        ),
                    )
                    await write_handover("blocked", artefacts={
                        "time_spent_seconds": elapsed,
                        "no_changes": True,
                        "spent_pence": result.spent_pence,
                        "block_reason": block_reason,
                    })
                    await client.set_status(task_id, "blocked")
                    await post_as_jarvis(
                        f"⚠️ Task #{task_id} ({backend_name}, `{repo}`) blocked — "
                        f"no shipped artefact ({block_reason}). _{elapsed}s_"
                    )
                    return

                await client.append_run(task_id, "jarvis_note", {
                    "message": "sub-agent finished with no file changes (allowed for this task)",
                })
                await write_handover("done", artefacts={
                    "time_spent_seconds": elapsed,
                    "no_changes": True,
                    "spent_pence": result.spent_pence,
                })
                await client.set_status(
                    task_id, "done",
                    spent_pence=result.spent_pence,
                    spent_tokens=result.spent_tokens,
                )
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

            await _record_deliverable_links(
                client, task_id, str(repo), branch=branch, pr_url=pr_url or None,
                no_changes=False,
            )

            # ── Deploy to VPS subdomain if it looks like a web project ──────
            deploy_url: str | None = None
            should_deploy = (
                settings.deploy_enabled
                and metadata.get("local_agent_deploy", True) is not False
                and await asyncio.to_thread(_looks_like_web_project, workspace)
            )
            if should_deploy:
                deploy_outcome = await _build_and_deploy_web_task(
                    client, task_id, workspace, str(repo), branch, metadata,
                )
                deploy_url = deploy_outcome.url
                if deploy_url:
                    await _record_deliverable_links(
                        client,
                        task_id,
                        str(repo),
                        branch=branch,
                        pr_url=pr_url or None,
                        deploy_url=deploy_url,
                        no_changes=False,
                    )

            elapsed = int(time.time() - started)
            commit_shas = await asyncio.to_thread(_head_commit_shas, workspace)
            # Phase 19 done-note (success path) — captures the
            # artefacts (PR URL, branch, deploy URL, time spent) and
            # asks the backend for a structured engineer summary.
            await write_handover("done", artefacts={
                "pr_url": pr_url,
                "branch": branch,
                "deploy_url": deploy_url,
                "commits": commit_shas,
                "time_spent_seconds": elapsed,
                "spent_pence": result.spent_pence,
            })

            await client.set_status(
                task_id, "done",
                spent_pence=result.spent_pence,
                spent_tokens=result.spent_tokens,
            )

            mins, secs = divmod(elapsed, 60)
            elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            msg = (
                f"✅ **Task #{task_id}** ({backend_name}, `{repo}`)\n"
                f"Branch `{branch}` · {elapsed_str}"
            )
            if pr_url:
                msg += f" · PR <{pr_url}>"
            if deploy_url:
                msg += f"\n🌐 Live: <{deploy_url}>"
            await post_as_jarvis(msg)

    except Exception as e:
        log.exception("run_job crashed")
        # Surface the actual exception text in the operator-visible activity
        # log so the user doesn't have to docker-exec into the agent to find
        # the traceback. Cap at 600 chars — long stack traces still go to
        # docker logs via log.exception above.
        exc_kind = type(e).__name__
        exc_msg = str(e)[:600] or "(no message)"
        # Self-diagnose common failures into a (diagnosis, recommended_action)
        # pair. When the heuristic fires we surface a useful sentence at the
        # top of the operator's activity log instead of just the traceback.
        interpretation = _interpret_error(e)
        if interpretation is not None:
            diagnosis, action = interpretation
            await client.append_run(
                task_id,
                "error",
                error_run_payload(
                    reason=exc_kind,
                    message=f"{diagnosis} → {action}",
                    error=exc_msg,
                    diagnosis=diagnosis,
                    recommended_action=action,
                    exception=str(e),
                    exception_type=exc_kind,
                ),
            )
        else:
            await client.append_run(
                task_id,
                "error",
                error_run_payload(
                    reason=exc_kind,
                    message=f"runner exception: {exc_kind}: {exc_msg}",
                    error=exc_msg,
                    exception=str(e),
                    exception_type=exc_kind,
                ),
            )
        # ── Hive failover ────────────────────────────────────────────
        # Before we mark this task properly blocked, decide whether
        # another worker should get a swing. Infrastructure errors
        # (git, gh, network) usually mean *this* worker is the broken
        # one, not the task. Self-exclude and re-queue so the next poll
        # by a healthy worker picks it up.
        rotated = False
        if _is_retryable_infra_error(e):
            try:
                # Re-read metadata fresh from the original claim payload;
                # mutating in place is fine because we don't need it again.
                existing_excluded = metadata.get("excluded_agents")
                if isinstance(existing_excluded, list):
                    excluded = [str(x) for x in existing_excluded if x]
                else:
                    excluded = []
                me = agent_id()
                retry_count = int(metadata.get("hive_retry_count") or 0)
                # Count the *unique* workers that have failed so far. We
                # rotate while there's still room under the cap AND we
                # haven't already excluded the current worker (which
                # would mean the claim filter is broken — bail to blocked
                # rather than spin forever).
                if me not in excluded and retry_count < HIVE_RETRY_MAX:
                    excluded.append(me)
                    rotation_reason = (
                        interpretation[0] if interpretation is not None
                        else f"{exc_kind}: {exc_msg[:200]}"
                    )
                    await client.merge_metadata(task_id, {
                        "excluded_agents": excluded,
                        "hive_retry_count": retry_count + 1,
                        "last_hive_rotation": {
                            "agent_id": me,
                            "reason": rotation_reason,
                            "exception_type": exc_kind,
                            "at": time.time(),
                        },
                    })
                    await client.append_run(task_id, "jarvis_note", {
                        "message": (
                            f"🔁 Infrastructure error on worker `{me}` "
                            f"({rotation_reason[:160]}). Rotating to another "
                            f"hive worker (attempt {retry_count + 1}/"
                            f"{HIVE_RETRY_MAX})."
                        ),
                        "rotation": {
                            "from_agent": me,
                            "attempt": retry_count + 1,
                            "max_attempts": HIVE_RETRY_MAX,
                        },
                    })
                    await client.set_status(task_id, "queued")
                    await post_as_jarvis(
                        f"🔁 Task #{task_id} rotated off `{me}` "
                        f"({exc_kind}). Another hive worker will pick it up."
                    )
                    rotated = True
                    log.info(
                        "task %s rotated off %s (attempt %d/%d): %s",
                        task_id, me, retry_count + 1, HIVE_RETRY_MAX, rotation_reason,
                    )
            except Exception:
                log.exception("hive rotation failed; falling through to blocked")

        if not rotated:
            # Phase 19 blocked-note (exception path). Best-effort; if note
            # generation itself fails too we still flip status + alert.
            try:
                note_artefacts: dict[str, Any] = {
                    "exception": str(e)[:400],
                    "exception_type": exc_kind,
                }
                if interpretation is not None:
                    note_artefacts["diagnosis"] = interpretation[0]
                    note_artefacts["recommended_action"] = interpretation[1]
                # Surface the hive history so the operator can see we tried.
                if metadata.get("excluded_agents"):
                    note_artefacts["hive_excluded_agents"] = metadata["excluded_agents"]
                    note_artefacts["hive_retry_count"] = int(
                        metadata.get("hive_retry_count") or 0
                    )
                await emit_handover_note(
                    client, task, "blocked", backend_name,
                    context={"user_steer": metadata.get("last_user_steer", "")},
                    artefacts=note_artefacts,
                )
            except Exception:
                log.exception("blocked-note emit failed during exception handler")
            await client.set_status(task_id, "blocked")
            # Discord alert — prefer the diagnosis when available.
            alert_summary = (
                f"`{interpretation[0]}`\n→ {interpretation[1]}"
                if interpretation is not None
                else f"`{exc_kind}: {str(e)[:200]}`"
            )
            hive_tail = ""
            if metadata.get("hive_retry_count"):
                hive_tail = (
                    f"\n_Hive: {metadata['hive_retry_count']} worker(s) tried "
                    f"and failed before giving up._"
                )
            await post_as_jarvis(
                f"💥 Task #{task_id} crashed in the local agent: {alert_summary}{hive_tail}"
            )
    finally:
        stop_hb.set()
        await hb_task
        bus.task_ended(task_id)
        inject_queues.pop(task_id, None)
        try:
            teardown_task_terminal(task_id)
        except Exception:
            log.exception("task %s terminal teardown failed", task_id)
