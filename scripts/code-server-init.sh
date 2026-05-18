#!/bin/bash
# code-server-init.sh
# Runs before code-server starts. Sets up the 'coder' user so the VS Code
# integrated terminal opens as non-root (required by claude --dangerously-skip-permissions).
set -e

# ── Link claude + ssh config into coder home ──────────────────────────────
mkdir -p /home/coder

# .claude.json (main config) - copy so coder can write to it
if [ -f /root/.claude.json ]; then
    cp /root/.claude.json /home/coder/.claude.json
fi

# .claude directory must be WRITABLE for `claude` to persist auth credentials
# and keybindings. The host mount at /root/.claude is read-only, so a symlink
# breaks /login. We seed a writable copy at /home/coder/.claude from the RO
# source on first boot. Persisted via the jarvis-coder-claude-data named
# volume so credentials survive `docker compose down`.
#
# If a symlink already exists (from older versions of this script), remove it.
if [ -L /home/coder/.claude ]; then
    rm /home/coder/.claude
fi
mkdir -p /home/coder/.claude

# Seed read-mostly items from the RO source if they're missing locally.
# We deliberately do NOT seed .credentials.json - users log in fresh.
if [ -d /root/.claude ]; then
    for item in CLAUDE.md hooks settings.json settings.local.json statsig commands plugins; do
        if [ -e "/root/.claude/$item" ] && [ ! -e "/home/coder/.claude/$item" ]; then
            cp -r "/root/.claude/$item" /home/coder/.claude/ 2>/dev/null || true
        fi
    done
fi

# .ssh (read-only mount from host)
if [ -d /root/.ssh ] && [ ! -L /home/coder/.ssh ]; then
    ln -sfn /root/.ssh /home/coder/.ssh
fi

# Lock down .claude ownership/perms so claude CLI accepts them (it warns on
# group/other-readable credential files). All chown calls are wrapped in
# `|| true` so a single failure can't abort the rest under `set -e`.
{ chown -R coder:coder /home/coder/.claude 2>/dev/null; } || true
{ chmod 700 /home/coder/.claude 2>/dev/null; } || true

# Disable -e for the home chown block - some filesystem combos (9p, overlayfs)
# return weird errno on chown of bind-mounted points, and we don't want that
# to kill the whole init.
set +e
chown coder:coder /home/coder 2>/dev/null
for f in /home/coder/.profile /home/coder/.claude.json /home/coder/.bashrc /home/coder/.bash_logout /home/coder/.ssh /home/coder/.npm-global; do
    if [ -e "$f" ] || [ -L "$f" ]; then
        chown -h coder:coder "$f" 2>/dev/null
    fi
done
# Belt-and-braces: anything else at the top level (cache dirs, npm dirs).
find /home/coder -mindepth 1 -maxdepth 1 \
    -not -path /home/coder/.claude \
    -exec chown -h coder:coder {} \; 2>/dev/null
set -e

# ── Write env vars into coder's profile ───────────────────────────────────
# (su - coder starts a login shell that doesn't inherit parent env)
PROFILE=/home/coder/.profile
# Clear previous auto-generated block
grep -v '# jarvis-auto' "$PROFILE" 2>/dev/null > /tmp/.profile.tmp || true
mv /tmp/.profile.tmp "$PROFILE" 2>/dev/null || true

for var in ANTHROPIC_API_KEY OPENAI_API_KEY OPENAI_BASE_URL OPENAI_MODEL GIT_SSH_COMMAND; do
    val="${!var}"
    if [ -n "$val" ]; then
        echo "export $var='$val' # jarvis-auto" >> "$PROFILE"
    fi
done
# npm global prefix in coder home so `npm install -g` never needs root
echo "export NPM_CONFIG_PREFIX=/home/coder/.npm-global # jarvis-auto" >> "$PROFILE"
echo "export PATH=/home/coder/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin # jarvis-auto" >> "$PROFILE"
echo "export HOME=/home/coder # jarvis-auto" >> "$PROFILE"

chown coder:coder "$PROFILE" 2>/dev/null || true

# ── Set VS Code terminal default to coder (only on first run) ──────────────
SETTINGS_DIR="/root/.local/share/code-server/User"
SETTINGS_FILE="$SETTINGS_DIR/settings.json"
mkdir -p "$SETTINGS_DIR"

if [ ! -f "$SETTINGS_FILE" ]; then
    cat > "$SETTINGS_FILE" << 'SETTINGS'
{
  "terminal.integrated.defaultProfile.linux": "coder",
  "terminal.integrated.profiles.linux": {
    "coder": {
      "path": "/bin/su",
      "args": ["-", "coder"],
      "icon": "account"
    },
    "root (admin)": {
      "path": "/bin/bash",
      "icon": "terminal-bash"
    }
  }
}
SETTINGS
fi

# ── Hand off to original code-server entrypoint ───────────────────────────────
exec /usr/bin/entrypoint.sh "$@"
