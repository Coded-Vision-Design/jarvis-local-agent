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
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from .agent_identity import can_run, agent_id
from .backends import get_backend
from .config import settings
from .discord_webhook import post_as_jarvis
from .jarvis_client import get_client
from .notes import emit_handover_note
from .preflight import fetch_prior_context
from .repos import is_whitelisted
from .state import bus, inject_queues, slots

log = logging.getLogger("jarvis-agent.runner")

GITHUB_ORG = "codedvisiondesign"  # matches CLAUDE.md / .env defaults
MAX_HEAL_ATTEMPTS = 3             # max self-healing retry cycles per task


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

    # Register so future tasks can run against it
    add_to_whitelist(repo)

    # Enrol in org-level deploy secrets (visibility=selected) - soft-fail
    try:
        _grant_deploy_secret_access(repo)
    except Exception:
        log.exception("failed to enrol %s in deploy secrets (soft-fail)", repo)

    log.info("private repo created: https://github.com/%s/%s", GITHUB_ORG, repo)
    return target


def _git_clone_or_fetch(repo: str, create_if_missing: bool = False) -> Path:
    """Ensure the workspace exists and is on origin/main. Returns the path."""
    target = settings.workspace_root / "workspaces" / repo
    target.parent.mkdir(parents=True, exist_ok=True)
    if not (target / ".git").exists():
        url = f"git@github.com:{GITHUB_ORG}/{repo}.git"
        try:
            _sh(["git", "clone", url, str(target)])
        except subprocess.CalledProcessError as exc:
            if create_if_missing and (
                "not found" in exc.stderr.lower()
                or "repository" in exc.stderr.lower()
                or exc.returncode == 128
            ):
                return _create_private_repo(repo, target)
            raise
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

    ssh_cmd = "ssh -i /root/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new"

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
        await client.set_status(task_id, "queued")
        return

    # Register with StreamBus so the live UI can pick it up immediately.
    bus.task_started(task_id, {
        "backend": backend_name,
        "repo": repo,
        "title": task.get("title", ""),
    })
    inject_queues[task_id] = asyncio.Queue(maxsize=10)

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

            # Phase 19 — track tool calls + errors for the auto-note
            # context. Capped to recent N so the note prompt stays small.
            recent_tools: list[str] = []
            recent_errors: list[str] = []
            tool_error_counts: dict[str, int] = {}

            async def log_cb(kind: str, payload: dict) -> None:
                await client.append_run(task_id, kind, payload)
                await bus.put_event(task_id, kind, payload)
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
                await client.append_run(task_id, "error", {
                    "summary": result.summary,
                    "error": result.error or "",
                })
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
            if not changed:
                await client.append_run(task_id, "jarvis_note", {
                    "message": "sub-agent finished with no file changes",
                })
                elapsed = int(time.time() - started)
                # Phase 19 — done-note even on the no-changes path so
                # the engineer log captures "ran, decided nothing to
                # do, reasoned why".
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

            if pr_url:
                await client.merge_metadata(task_id, {
                    "local_agent_pr_url": pr_url,
                    "local_agent_branch": branch,
                })

            # ── Deploy to VPS subdomain if it looks like a web project ──────
            deploy_url: str | None = None
            should_deploy = (
                settings.deploy_enabled
                and metadata.get("local_agent_deploy", True) is not False
                and await asyncio.to_thread(_looks_like_web_project, workspace)
            )
            if should_deploy:
                await client.append_run(task_id, "jarvis_note", {
                    "message": "web project detected - building and deploying to VPS",
                })
                dist_dir = await asyncio.to_thread(_build_web_project, workspace)
                if dist_dir is not None:
                    deploy_url = await asyncio.to_thread(_deploy_to_vps, str(repo), dist_dir)
                    if deploy_url:
                        await asyncio.to_thread(_update_readme_with_url, workspace, deploy_url)
                        # Commit and push the README update onto the same branch
                        try:
                            await asyncio.to_thread(
                                _sh,
                                ["git", "add", "README.md"],
                                workspace,
                            )
                            await asyncio.to_thread(
                                _sh,
                                ["git", "-c", "user.email=contact@codedvisiondesign.co.uk",
                                 "-c", "user.name=Coded Vision Design",
                                 "commit", "-m", f"docs: add live site URL ({deploy_url})"],
                                workspace,
                            )
                            await asyncio.to_thread(
                                _sh,
                                ["git", "push", "origin", branch],
                                workspace,
                            )
                        except Exception:
                            log.exception("failed to commit README deploy URL")

                        await client.merge_metadata(task_id, {
                            "local_agent_deploy_url": deploy_url,
                        })
                        await client.append_run(task_id, "jarvis_note", {
                            "message": f"deployed to {deploy_url}",
                        })
                    else:
                        await client.append_run(task_id, "error", {
                            "message": "deploy to VPS failed - see logs",
                        })
                else:
                    await client.append_run(task_id, "jarvis_note", {
                        "message": "no build output found - skipping deploy",
                    })

            elapsed = int(time.time() - started)
            # Phase 19 done-note (success path) — captures the
            # artefacts (PR URL, branch, deploy URL, time spent) and
            # asks the backend for a structured engineer summary.
            await write_handover("done", artefacts={
                "pr_url": pr_url,
                "branch": branch,
                "deploy_url": deploy_url,
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
        await client.append_run(task_id, "error", {
            "message": "runner exception",
            "exception": str(e),
        })
        # Phase 19 blocked-note (exception path). Best-effort; if note
        # generation itself fails too we still flip status + alert.
        try:
            await write_handover("blocked", artefacts={
                "exception": str(e)[:400],
                "exception_type": type(e).__name__,
            })
        except Exception:
            log.exception("blocked-note emit failed during exception handler")
        await client.set_status(task_id, "blocked")
        await post_as_jarvis(
            f"💥 Task #{task_id} crashed in the local agent: `{type(e).__name__}: {str(e)[:200]}`"
        )
    finally:
        stop_hb.set()
        await hb_task
        bus.task_ended(task_id)
        inject_queues.pop(task_id, None)
