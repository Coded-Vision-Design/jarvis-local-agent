"""Drive `_git_clone_or_fetch` against the real test-website remote to
confirm the self-heal lands the missing seed push on GitHub.

Run inside the agent container:
  docker exec jarvis-agent python /workspace/agent/scripts/verify_test_website_heal.py
"""
import subprocess
import sys

sys.path.insert(0, "/workspace/agent")

from src import runner  # noqa: E402

REPO = "test-website"

print(f"[before] running _git_clone_or_fetch against real {REPO!r} remote...")
path = runner._git_clone_or_fetch(REPO)
print(f"[after]  workspace ready at: {path}")

ls = subprocess.run(
    ["git", "-C", str(path), "ls-remote", "--heads", "origin"],
    capture_output=True, text=True,
)
print(f"[remote-refs] stdout={ls.stdout!r} stderr={ls.stderr[:200]!r}")

br = subprocess.run(
    ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
    capture_output=True, text=True,
)
print(f"[head] {br.stdout.strip()}")

local = subprocess.run(
    ["git", "-C", str(path), "rev-parse", "HEAD"],
    capture_output=True, text=True,
).stdout.strip()
remote = subprocess.run(
    ["git", "-C", str(path), "rev-parse", "origin/main"],
    capture_output=True, text=True,
).stdout.strip()
print(f"[local-head]  {local}")
print(f"[origin/main] {remote}")
assert local and local == remote, "local main and origin/main should agree after heal"
print("OK — heal verified, local main == origin/main on GitHub")
