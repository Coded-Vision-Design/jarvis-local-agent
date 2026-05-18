"""NitroGen control facility.

NitroGen is not a coding backend - it's a game-playing model that Jarvis can
launch on demand. This module exposes:

  - play(game_exe, duration_s, kb_mouse)   start playing a Windows game
  - stop()                                 stop the current session
  - status()                               read what's happening now
  - is_available()                         can we even talk to it?

Architecture:
  Agent (Linux container) writes a JSON command file to /workspace/nitrogen/commands/.
  Windows host runs nitrogen-host-watcher.ps1 which polls that directory,
  spawns play.py against the named game, and writes status to
  /workspace/nitrogen/status/current.json. The agent reads status from there.

  The inference half (NitroGen 500M DiT model) lives in the jarvis-nitrogen
  container on port 5555 (ZMQ). play.py on the host connects to it via
  host.docker.internal:5555 -> the published port.

This module touches only the agent side of the command bus. Starting/stopping
the Docker container is handled separately by docker compose."""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis-agent.backend.nitrogen")

# Shared via the /workspace mount: agent writes, host watcher reads.
COMMANDS_DIR = Path("/workspace/nitrogen/commands")
STATUS_FILE = Path("/workspace/nitrogen/status/current.json")


def _ensure_dirs() -> None:
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)


def _write_command(cmd: dict[str, Any]) -> str:
    """Drop a command file with a uuid name. Returns the id."""
    _ensure_dirs()
    cid = uuid.uuid4().hex[:12]
    path = COMMANDS_DIR / f"{cid}.json"
    path.write_text(json.dumps(cmd), encoding="utf-8")
    log.info("wrote nitrogen command %s: %s", cid, cmd)
    return cid


def status() -> dict[str, Any]:
    """Read the most recent status the host watcher wrote.

    Returns a stable shape even if the watcher has never run."""
    _ensure_dirs()
    if not STATUS_FILE.exists():
        return {
            "state": "unknown",
            "detail": "host watcher has never written status. "
                      "Start it with: pwsh C:/Jarvis/agent/scripts/nitrogen-host-watcher.ps1",
        }
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"state": "error", "detail": f"could not read status: {exc}"}


def is_available() -> bool:
    """Cheap probe: did the host watcher heartbeat recently?

    True if the status file was updated in the last 60 seconds."""
    if not STATUS_FILE.exists():
        return False
    try:
        age = time.time() - STATUS_FILE.stat().st_mtime
        return age < 60
    except Exception:
        return False


def play(
    game_exe: str,
    *,
    duration_s: int = 0,
    kb_mouse: bool = False,
) -> dict[str, Any]:
    """Start a NitroGen session against `game_exe`.

    Args:
        game_exe: executable name (e.g. "notepad.exe") or full path.
        duration_s: auto-stop after this many seconds. 0 = run until stopped.
        kb_mouse: use keyboard+mouse mode instead of gamepad (weaker model perf).

    The actual launch happens on the Windows host. This function only
    queues the command; check status() to see when it took effect."""
    cid = _write_command({
        "cmd": "play",
        "game": game_exe,
        "duration_s": duration_s,
        "kb_mouse": kb_mouse,
    })
    return {"command_id": cid, "queued": True, "game": game_exe}


def stop() -> dict[str, Any]:
    """Stop the current NitroGen session."""
    cid = _write_command({"cmd": "stop"})
    return {"command_id": cid, "queued": True}


def wait_for_state(target: str, *, timeout_s: float = 30.0, poll_s: float = 1.0) -> dict[str, Any]:
    """Block until status.state == target (or timeout). Returns the final status."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = status()
        if st.get("state") == target:
            return st
        time.sleep(poll_s)
    return status()
