"""Make `src` importable as `src` without installing the package."""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Pydantic-settings reads the real env at import time. Neutralise the
# couple of vars that would otherwise pull live secrets into tests.
os.environ.setdefault("JARVIS_LOCAL_AGENT_TOKEN", "test-token")
os.environ.setdefault("GITHUB_TOKEN", "test-gh-token")
os.environ.setdefault("JARVIS_AGENT_REPO_WRITE_TOKEN", "test-write-token")
