# jarvis-local-agent

The local sub-agent for delegated coding work. Runs on the Windows dev box, polls the Jarvis VPS for delegated tasks, and executes them across four backends (Claude, Qwen, Hermes, Smart) with self-healing tests, approval gates, and auto-deploy to wildcard subdomains.

## Quick links

- **[ARCHITECTURE.md](./ARCHITECTURE.md)** - how everything fits together (diagrams, ports, flow)
- **[ROLLOUT.md](./ROLLOUT.md)** - one-time setup steps (vLLM proxy, GH secrets, ComfyUI)
- **[COMFYUI.md](./COMFYUI.md)** - image generation runbook
- **[prompts/CODING_STANDARDS.md](./prompts/CODING_STANDARDS.md)** - mandatory rules every agent follows (responsive, sticky header, no-hscroll, TDD, OWASP, layered tests)
- **[templates/CLAUDE.md](./templates/CLAUDE.md)** - auto-seeded into every workspace
- **[prompts/qwen-orchestrator.md](./prompts/qwen-orchestrator.md)** - Cline system prompt

## What's in the box

| Component | Port | Purpose |
|-----------|------|---------|
| jarvis-agent | 17920 | FastAPI + MCP + `/ui` dashboard + smart router + self-healing |
| jarvis-code-server | 17921 | VS Code in browser, Cline + claude/qwen/codex CLIs |
| jarvis-php | 17922 | Apache, serves `/workspace/htdocs` |
| jarvis-db | 17923 | MariaDB 10.6 |
| jarvis-redis | 17924 | Redis 7 |
| comfyui-proxy | 17926 | Wake-on-request ComfyUI (optional overlay) |
| vLLM idle proxy | 18000 | Transparent proxy in front of vLLM (host process) |
| vLLM real | 18001 | Qwen3-14B-AWQ (wakes on demand) |
| Hermes (optional) | 18002 | Second vLLM for creative/planning tasks |

All bound to `127.0.0.1` only. Never expose any of these publicly.

## Setup

```powershell
cd C:\Jarvis\agent
copy .env.example .env
# Edit .env - paste JARVIS_LOCAL_AGENT_TOKEN, ANTHROPIC_API_KEY,
# DISCORD_JARVIS_TASKS_WEBHOOK_URL, GITHUB_TOKEN

docker compose up -d --build

# Open the dashboard:
start http://127.0.0.1:17920/ui

# Open VS Code in browser:
start http://127.0.0.1:17921
```

Then follow [ROLLOUT.md](./ROLLOUT.md) for:
1. vLLM idle proxy (host scheduled task)
2. GitHub org secrets (one-time setup)
3. Health loop activation (optional)
4. ComfyUI (optional)

## How a task flows

1. **You** create a task on the Jarvis web UI, or via Cline (`create_task` MCP tool), or a Jarvis Discord command.
2. **VPS** queues it.
3. **Agent** polls every 5 s, claims the task.
4. **Runner** clones the repo (or `gh repo create --private` if needed), seeds `CLAUDE.md`, picks a backend.
5. **Smart router** scores the task by complexity:
   - Planning keywords → Hermes (free, local)
   - Complex (score ≥ 0.5) → Claude (or Qwen on quota fallback)
   - Simple → Qwen
6. **Backend** runs the task in the repo workspace with stdin injection support.
7. **Self-healing loop** runs smoke → unit → integration → regression (regression only on big-change tasks). On failure, retries the backend with the test output appended (up to 3 attempts).
8. **Web project?** `npm run build` → rsync `dist/` to `<repo>.codedvisiondesign.co.uk` via the existing Claudia wildcard pipeline.
9. **Git** commit, push, open PR via gh CLI.
10. **Discord** notification with the PR URL and the live site URL.

## Where the safety lives

- **Approval queue** - health-loop findings and any `requires_approval: true` task waits in `/approvals` until you sign off (or batch-approve via UI / Cline / REST).
- **E2E sessions** - opt-in auto-approve for a bounded batch (`max_tasks`, `duration_seconds`).
- **CLAUDE.md mandatory rules** in every workspace - responsive, sticky header, no h-scroll, WCAG AA, British English, no em-dashes.
- **Org-level deploy secrets** with `visibility=selected` - only Jarvis-built prospect repos can read them. Client repos are isolated.
- **Self-healing tests** - smoke layer fails fast; regression layer triggers automatically on refactor/migrate/redesign keywords.
- **`--dangerously-skip-permissions`** is blocked for root - the code-server terminal runs as `coder` user (uid 1000).

## Editing the whitelist

`repos.yml` is re-read on every poll cycle. Edit, save, no restart needed.

When Jarvis creates a private repo via `local_agent_create_repo: true`, it adds the repo to `repos.yml` automatically.

## Hive mind — propagating changes across workers

A push to `main` that touches `Dockerfile`, `code-server.Dockerfile`,
`requirements.txt`, `src/`, `claude-config/`, `prompts/`, `jarvis_mcp/`,
`scripts/`, `templates/`, or `hooks/` triggers `.github/workflows/build-image.yml`
which builds both images and pushes to GHCR:

- `ghcr.io/codedvisiondesign/jarvis-local-agent:latest` + `:sha-<short>`
- `ghcr.io/codedvisiondesign/jarvis-code-server:latest` + `:sha-<short>`

**Adding a new worker host** (VPS, laptop, etc.):

```bash
# 1. Log in to GHCR with a read:packages PAT (one time per host).
echo $GHCR_PAT | docker login ghcr.io -u codedvisiondesign --password-stdin

# 2. Compose pull + up. The :latest images come down.
docker compose pull && docker compose up -d

# 3. claude login + codex login (one time per host). OAuth tokens land
#    on the mounted /root/.claude volume and persist across image upgrades.
docker exec -it jarvis-agent claude login
docker exec -it jarvis-agent codex login
```

**Updating an existing host after a push to main:**

```bash
docker compose pull && docker compose up -d
```

**Skills / CLAUDE.md / slash commands** — drop them into `claude-config/`,
push, both images rebuild with the new content. Brand-new volumes pick
them up; existing volumes (with your OAuth token already in them)
preserve their current content (Docker only seeds named volumes on
first create — by design, so an image refresh never overwrites your
login state).

**Why per-host OAuth rather than synced tokens** — Anthropic's
subscription auth uses device-scoped refresh tokens. Sharing one across
hosts risks anomaly-detection lockouts on the account. The 30 seconds
of `claude login` per new host is the right trade.
