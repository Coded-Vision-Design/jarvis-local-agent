#!/bin/bash
# claude-delegate.sh — Qwen calls this to delegate a task to Claude Code.
# Usage: claude-delegate.sh "task description"
# Output: streams Claude's progress, exits 0 on success.

set -euo pipefail

if [ $# -eq 0 ]; then
  echo "Usage: claude-delegate.sh <task>" >&2
  exit 1
fi

TASK="$*"
REPO_DIR="${PWD}"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Claude Code — delegated task"
echo "  Dir: ${REPO_DIR}"
echo "  Task: ${TASK:0:120}..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

claude --dangerously-skip-permissions -p "$TASK"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Claude Code — done"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
