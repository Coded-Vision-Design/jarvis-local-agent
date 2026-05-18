"""Android build + test facility for the agent.

The agent edits Kotlin/Java source in /workspace, runs gradle to build,
and shells out to adb to install + test on a host-side emulator or
USB-connected device.

Split:
  Agent container         Windows host
  ───────────────         ─────────────
  edit .kt / .java                                     (already mounted)
  gradle build / assemble                              (in container, native)
  adb install / shell  ── tcp:5037 ──▶  adb-server     (on host)
                                              │
                                              ▼
                                       Android emulator (Android Studio)
                                       or USB-attached device

The host runs `adb -a -P 5037 server nodaemon` (auto-installed by
scripts/setup-android-host.ps1) so the container's adb can reach it via
host.docker.internal:5037. ADB_SERVER_SOCKET env in the code-server image
already points there.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis-agent.backend.android")

ADB_SERVER_SOCKET = os.environ.get("ADB_SERVER_SOCKET", "tcp:host.docker.internal:5037")

# Shared command bus with the Windows host watcher
HOST_COMMANDS_DIR = Path("/workspace/android/commands")
HOST_STATUS_FILE = Path("/workspace/android/status/current.json")
EMULATOR_OUTPUT_DIR = Path("/workspace/android/screenshots")


def _ensure_dirs() -> None:
    HOST_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    HOST_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMULATOR_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _write_host_command(cmd: dict[str, Any]) -> str:
    """Queue a command for the Windows host watcher to pick up."""
    _ensure_dirs()
    cid = uuid.uuid4().hex[:12]
    (HOST_COMMANDS_DIR / f"{cid}.json").write_text(json.dumps(cmd), encoding="utf-8")
    return cid


def host_status() -> dict[str, Any]:
    """Read the most recent host watcher status."""
    _ensure_dirs()
    if not HOST_STATUS_FILE.exists():
        return {
            "state": "unknown",
            "detail": "Android host watcher not running. "
                      "Run: pwsh C:/Jarvis/agent/scripts/install-android-watcher.ps1",
        }
    try:
        return json.loads(HOST_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"state": "error", "detail": f"could not read status: {exc}"}


async def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> tuple[int, str, str]:
    """Run a subprocess, return (rc, stdout, stderr). Bounded by timeout."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "ADB_SERVER_SOCKET": ADB_SERVER_SOCKET},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", f"timeout after {timeout}s"
    return proc.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def is_android_project(workspace: Path) -> bool:
    """Heuristic: does this look like an Android project?"""
    if (workspace / "settings.gradle.kts").exists() or (workspace / "settings.gradle").exists():
        # Look for an android plugin in any build file
        for f in workspace.rglob("build.gradle*"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                if "com.android.application" in text or "com.android.library" in text:
                    return True
            except Exception:
                continue
    return False


async def list_devices() -> dict[str, Any]:
    """adb devices - returns list of connected emulators/devices on the host."""
    if shutil.which("adb") is None:
        return {"ok": False, "error": "adb not installed - rebuild code-server image"}
    rc, out, err = await _run(["adb", "devices", "-l"], timeout=10)
    devices = []
    for line in out.splitlines()[1:]:  # skip "List of devices attached"
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append({"id": parts[0], "state": parts[1], "info": " ".join(parts[2:])})
    return {"ok": rc == 0, "devices": devices, "rc": rc, "stderr_tail": err[-500:]}


async def build_apk(
    workspace: Path,
    variant: str = "Debug",
    *,
    clean: bool = False,
) -> dict[str, Any]:
    """Run `./gradlew assemble<Variant>` (or `gradle` if no wrapper).

    Returns the apk path on success.
    """
    if not is_android_project(workspace):
        return {"ok": False, "error": f"{workspace} doesn't look like an Android project"}

    gradle_cmd = "./gradlew" if (workspace / "gradlew").exists() else "gradle"
    args = []
    if clean:
        args.append("clean")
    args.append(f"assemble{variant}")

    rc, out, err = await _run([gradle_cmd, *args], cwd=workspace, timeout=900)
    if rc != 0:
        return {"ok": False, "error": f"build failed rc={rc}", "stderr_tail": err[-2000:], "stdout_tail": out[-2000:]}

    # Find produced apk
    apks = list((workspace).glob(f"**/build/outputs/apk/{variant.lower()}/*.apk"))
    if not apks:
        apks = list((workspace).glob(f"**/build/outputs/apk/**/*.apk"))

    return {
        "ok": True,
        "variant": variant,
        "apk_path": str(apks[0]) if apks else None,
        "apk_count": len(apks),
    }


async def run_unit_tests(workspace: Path, variant: str = "Debug") -> dict[str, Any]:
    """Run `./gradlew test<Variant>UnitTest` - JVM-only tests, no device needed."""
    if not is_android_project(workspace):
        return {"ok": False, "error": "not an Android project"}
    gradle_cmd = "./gradlew" if (workspace / "gradlew").exists() else "gradle"
    rc, out, err = await _run(
        [gradle_cmd, f"test{variant}UnitTest", "--no-daemon", "--console=plain"],
        cwd=workspace,
        timeout=600,
    )
    return {
        "ok": rc == 0,
        "rc": rc,
        "stdout_tail": out[-2000:],
        "stderr_tail": err[-2000:],
    }


async def install_apk(apk_path: str, device_id: str | None = None) -> dict[str, Any]:
    """adb install (-r to reinstall, -t to allow test apks)."""
    if not Path(apk_path).exists():
        return {"ok": False, "error": f"APK not found at {apk_path}"}
    args = ["adb"]
    if device_id:
        args += ["-s", device_id]
    args += ["install", "-r", "-t", apk_path]
    rc, out, err = await _run(args, timeout=120)
    return {
        "ok": rc == 0 and "Success" in out,
        "rc": rc,
        "stdout_tail": out[-1000:],
        "stderr_tail": err[-500:],
    }


async def is_android_available() -> bool:
    """Capability probe: is adb installed + host server reachable?"""
    if shutil.which("gradle") is None and shutil.which("./gradlew") is None:
        # Gradle absent - this container can't build
        return False
    if shutil.which("adb") is None:
        return False
    rc, _, _ = await _run(["adb", "version"], timeout=5)
    return rc == 0


# ── Host-side actions (via the file-based command bus) ───────────────────────

def launch_studio(project_path: str | None = None) -> dict[str, Any]:
    """Open Android Studio on the Windows host, optionally at a project.

    Returns immediately - the watcher picks up the command within ~2 s.
    Check `host_status()` to confirm it launched."""
    cid = _write_host_command({"cmd": "start_studio", "project": project_path})
    log.info("queued start_studio: project=%s id=%s", project_path, cid)
    return {"command_id": cid, "queued": True, "project": project_path}


def start_emulator(avd: str | None = None) -> dict[str, Any]:
    """Start an Android emulator on the Windows host.

    If `avd` is omitted, the watcher picks the first available AVD.
    The emulator runs on the host (KVM/WHPX hardware accel), and once booted
    is reachable from this container via adb over host.docker.internal:5037."""
    cid = _write_host_command({"cmd": "start_emulator", "avd": avd})
    log.info("queued start_emulator: avd=%s id=%s", avd, cid)
    return {"command_id": cid, "queued": True, "avd": avd}


def stop_emulator(device_id: str | None = None) -> dict[str, Any]:
    """Stop a named emulator (or all tracked emulators if device_id omitted)."""
    cid = _write_host_command({"cmd": "stop_emulator", "device_id": device_id})
    return {"command_id": cid, "queued": True, "device_id": device_id}


async def list_avds() -> dict[str, Any]:
    """List configured AVDs on the host (via the local emulator binary, if
    present; falls back to status-file hint if container lacks it)."""
    if shutil.which("emulator") is not None:
        rc, out, err = await _run(["emulator", "-list-avds"], timeout=10)
        avds = [line.strip() for line in out.splitlines() if line.strip()]
        return {"ok": rc == 0, "avds": avds}
    # No emulator binary in this container - fall back to whatever the host
    # watcher reported on its last update.
    st = host_status()
    return {
        "ok": False,
        "avds": [],
        "detail": "emulator binary not in container; ask the host watcher to "
                  "report. Status snapshot: " + json.dumps(st)[:300],
    }


async def wait_for_device(timeout_s: int = 120) -> dict[str, Any]:
    """Block until at least one device shows in `adb devices` with state=device.

    Useful right after `start_emulator()` so the next install/launch step
    doesn't race the boot."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = await list_devices()
        devices = [d for d in result.get("devices", []) if d.get("state") == "device"]
        if devices:
            return {"ok": True, "device": devices[0], "all": devices}
        await asyncio.sleep(3)
    return {"ok": False, "error": f"no device ready after {timeout_s}s"}


# ── Visual verification ───────────────────────────────────────────────────────

async def screenshot(
    device_id: str | None = None,
    out_path: str | None = None,
) -> dict[str, Any]:
    """Capture the current emulator/device screen via `adb shell screencap`.

    Pulls the PNG into /workspace/android/screenshots/ (visible on Windows at
    C:\\Jarvis\\android\\screenshots\\). Returns the absolute path."""
    if shutil.which("adb") is None:
        return {"ok": False, "error": "adb not in container - rebuild code-server image"}

    _ensure_dirs()
    if not out_path:
        out_path = str(EMULATOR_OUTPUT_DIR / f"screen_{int(time.time())}.png")

    base = ["adb"]
    if device_id:
        base += ["-s", device_id]

    # Capture on the device side, then pull
    rc1, _, err1 = await _run(base + ["shell", "screencap", "-p", "/sdcard/_jarvis_screen.png"], timeout=15)
    if rc1 != 0:
        return {"ok": False, "error": "screencap failed", "stderr_tail": err1[-500:]}

    rc2, _, err2 = await _run(base + ["pull", "/sdcard/_jarvis_screen.png", out_path], timeout=15)
    if rc2 != 0:
        return {"ok": False, "error": "adb pull failed", "stderr_tail": err2[-500:]}

    # Clean up the on-device temp file
    await _run(base + ["shell", "rm", "/sdcard/_jarvis_screen.png"], timeout=10)

    return {"ok": True, "path": out_path}


async def verify_visual(
    expectation_prompt: str,
    *,
    device_id: str | None = None,
    capture_path: str | None = None,
) -> dict[str, Any]:
    """End-to-end visual check: take a screenshot, send to the local vision
    model with a question/expectation, return the description.

    Cline-style usage: 'is the login button visible and tappable?' →
    captures emulator screen → MiniCPM-V answers in plain English.
    """
    shot = await screenshot(device_id=device_id, out_path=capture_path)
    if not shot.get("ok"):
        return {"ok": False, "error": "screenshot failed", "screenshot_error": shot}

    try:
        from .vision import describe_image, is_vision_available  # local import to avoid circular
    except Exception as exc:
        return {"ok": False, "error": f"vision backend unavailable: {exc}"}

    if not await is_vision_available():
        return {
            "ok": False,
            "error": "Vision backend not running on :18003.",
            "screenshot_path": shot["path"],
            "fix": "Start MiniCPM-V: wsl sudo bash /mnt/c/Jarvis/agent/scripts/start-vision-fg.sh",
        }

    description = await describe_image(shot["path"], prompt=expectation_prompt)
    return {
        "ok": True,
        "screenshot_path": shot["path"],
        "expectation": expectation_prompt,
        "description": description,
    }
