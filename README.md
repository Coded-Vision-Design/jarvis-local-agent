# jarvis-local-agent

Local sub-agent for delegated coding work. Runs on the Windows dev box, polls
Jarvis for `metadata.delegated_to = "local-code-agent"` tasks, and executes
them with either Claude Code headless or qwen-code against a local
Qwen3-Coder-14B served by vLLM on the host.

See the master plan: `C:\Users\djohn\.claude\plans\invoke-question-was-a-unified-barto.md`.

## Ports

Both bound to loopback only — never exposed on LAN.

| Service | Port | Why this port |
|---|---|---|
| `jarvis-agent` (FastAPI + MCP) | `127.0.0.1:17920` | Out of common dev/service ranges (XAMPP 80/443/3306, Postgres 5432, Redis 6379, Ollama 11434, Vite/Next/Flask/Django 3000–8080) |
| `vllm` (OpenAI-compat API, host-native) | `127.0.0.1:18000` | Same reason — vLLM's default 8000 conflicts too easily |

## Setup (one-time)

### 1. vLLM + Qwen3-Coder on the Windows host (not in Docker)

vLLM runs native on Windows so the RTX 5070 Ti gets CUDA directly without
Blackwell-passthrough issues seen with current Ollama.

```powershell
# In a fresh venv, e.g. C:\Jarvis\vllm-venv
python -m venv C:\Jarvis\vllm-venv
C:\Jarvis\vllm-venv\Scripts\activate
pip install vllm

# Pull the model (first run downloads ~9 GB)
vllm serve Qwen/Qwen3-Coder-14B-Instruct `
    --port 18000 `
    --host 127.0.0.1 `
    --quantization awq `
    --max-model-len 16384
```

Confirm: `curl http://127.0.0.1:18000/v1/models` returns a JSON model list.

Once happy, promote to a Windows scheduled task (Task Scheduler → "At log on")
so it auto-starts on reboot.

### 2. Agent container

```powershell
cd C:\Jarvis\agent
copy .env.example .env
# Edit .env: paste JARVIS_LOCAL_AGENT_TOKEN, ANTHROPIC_API_KEY,
# DISCORD_JARVIS_TASKS_WEBHOOK_URL (copy from c:\xampp\htdocs\Jarvis\.env)
docker compose up -d --build
curl http://127.0.0.1:17920/health
```

Expect: `{"ok":true,"vllm_reachable":true,"claude_ok":true,"repos_count":6}`.

### 3. Pair with Jarvis

Add the matching `JARVIS_LOCAL_AGENT_TOKEN` to `c:\xampp\htdocs\Jarvis\.env`
so the Jarvis server accepts the agent's polls. Sync to the VPS via
`.github/workflows/sync-secrets.yml`.

## Editing the whitelist

`repos.yml` is re-read on every poll cycle. Edit, save, no restart.

## Local IDE tip

The host has Python 3.14 with no project venv, so your IDE will flag
`fastapi` / `fastapi_mcp` as missing. If you want clean IDE diagnostics:

```powershell
python -m venv C:\Jarvis\agent\.venv
C:\Jarvis\agent\.venv\Scripts\activate
pip install -r requirements.txt
```

Then point the IDE at `C:\Jarvis\agent\.venv\Scripts\python.exe`. The container
ignores the venv entirely (it uses its own image-installed deps).
