#!/bin/bash
# /entrypoint.d/10-fix-coder-perms.sh
#
# Runs AFTER `fixuid` and AFTER our main init script. The base image's
# fixuid step chowns /home/coder back to whichever uid it thinks should
# own it, undoing the chown we did in code-server-init.sh. This script
# is dropped into ENTRYPOINTD (/entrypoint.d) which the base entrypoint
# walks at the very end, just before `exec dumb-init code-server`.
#
# Idempotent. Tolerates partial state.

# Don't kill the entrypoint if a single chown fails.
set +e

# Make sure /home/coder belongs to coder again
chown coder:coder /home/coder 2>/dev/null

# Top-level dotfiles - non-recursive to avoid descending into the
# .claude volume mount (which already has correct perms).
for f in /home/coder/.profile /home/coder/.claude.json /home/coder/.bashrc \
         /home/coder/.bash_logout /home/coder/.ssh /home/coder/.npm-global \
         /home/coder/.config /home/coder/.local /home/coder/.cache; do
    if [ -e "$f" ] || [ -L "$f" ]; then
        chown -h coder:coder "$f" 2>/dev/null
    fi
done

# Make sure .claude (volume) is coder-owned at the top level. We don't
# recurse - the contents already have the right perms from the seed step.
chown coder:coder /home/coder/.claude 2>/dev/null
chmod 700 /home/coder/.claude 2>/dev/null

# Create .local/bin (Claude doctor warns if missing) + .codex (OAuth tokens
# saved here by `codex login`).
for d in /home/coder/.local /home/coder/.local/bin /home/coder/.codex; do
    if [ ! -d "$d" ]; then
        mkdir -p "$d"
        chown coder:coder "$d"
    fi
done
chmod 700 /home/coder/.codex 2>/dev/null

# Install @openai/codex if missing (uses coder's npm prefix at .npm-global).
# Tolerates network unavailability - just skips silently.
if ! su - coder -c 'command -v codex >/dev/null 2>&1'; then
    su - coder -c 'npm install -g @openai/codex 2>/dev/null' || true
fi

# OAuth callback proxy: codex login binds to 127.0.0.1:1455 inside the
# container, which Docker NAT can't deliver to. We publish host:1455 to
# container:11455 (see docker-compose.yml), then this socat process forwards
# 0.0.0.0:11455 -> 127.0.0.1:1455 so the standard codex login flow works.
#
# Same pattern for any other OAuth callback that uses :54321 (e.g. Codex VS
# Code extension): publish host:54321 -> container:54322, forward 54322 -> 54321.
if command -v socat >/dev/null 2>&1; then
    # Kill any stale proxies from previous runs
    pkill -f 'socat.*:11455' 2>/dev/null || true
    pkill -f 'socat.*:54322' 2>/dev/null || true

    # Start the new forwarders in the background. /var/log persists nothing
    # important - log to /tmp so any crash trace is grabbable.
    nohup socat TCP-LISTEN:11455,bind=0.0.0.0,reuseaddr,fork TCP:127.0.0.1:1455 \
        > /tmp/socat-1455.log 2>&1 &
    nohup socat TCP-LISTEN:54322,bind=0.0.0.0,reuseaddr,fork TCP:127.0.0.1:54321 \
        > /tmp/socat-54321.log 2>&1 &
fi

exit 0
