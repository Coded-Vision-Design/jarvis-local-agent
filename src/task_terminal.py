"""Per-task terminal artefacts: rolling log file + optional tmux tail mirror."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def task_log_path(task_id: int) -> Path:
    return Path(f"/tmp/jarvis-task-{task_id}.log")


def task_tmux_name(task_id: int) -> str:
    return f"jarvis-task-{task_id}"


def setup_task_terminal(task_id: int) -> dict[str, str | None]:
    """Create an empty log file; optionally start a detached tmux tail session."""
    log_path = task_log_path(task_id)
    log_path.write_text("", encoding="utf-8")
    meta: dict[str, str | None] = {
        "local_agent_task_log": str(log_path),
        "local_agent_tmux_session": None,
    }
    if shutil.which("tmux"):
        name = task_tmux_name(task_id)
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                name,
                "tail",
                "-n",
                "+1",
                "-f",
                str(log_path),
            ],
            check=False,
        )
        meta["local_agent_tmux_session"] = name
    return meta


def append_task_log(task_id: int, line: str) -> None:
    if not line:
        return
    with task_log_path(task_id).open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def teardown_task_terminal(task_id: int) -> None:
    if not shutil.which("tmux"):
        return
    subprocess.run(
        ["tmux", "kill-session", "-t", task_tmux_name(task_id)],
        check=False,
    )
