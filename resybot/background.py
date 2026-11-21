"""
Run `resybot serve` in the background on the laptop.

The bot keeps going after you close the terminal or quit the menus, and
`caffeinate` keeps the Mac awake while it runs (-i: no idle sleep;
-s: no system sleep while on power). A closed lid on battery still
sleeps; that's macOS, not something an app can override.

State lives in logs/bot.pid; output in logs/bot.out.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

PID_FILE = Path("logs/bot.pid")
OUT_FILE = Path("logs/bot.out")


def status() -> dict:
    """{'running': bool, 'pid': int|None, 'since': datetime|None}"""
    if not PID_FILE.exists():
        return {"running": False, "pid": None, "since": None}
    try:
        info = json.loads(PID_FILE.read_text())
        pid = int(info["pid"])
    except (ValueError, KeyError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return {"running": False, "pid": None, "since": None}
    if not _alive(pid):
        PID_FILE.unlink(missing_ok=True)
        return {"running": False, "pid": None, "since": None}
    return {"running": True, "pid": pid, "since": datetime.fromtimestamp(info.get("started", time.time()))}


def start() -> dict:
    st = status()
    if st["running"]:
        return st
    PID_FILE.parent.mkdir(exist_ok=True)
    cmd = [sys.executable, "-m", "resybot", "serve"]
    if shutil.which("caffeinate"):
        cmd = ["caffeinate", "-is"] + cmd
    with OUT_FILE.open("a") as out:
        out.write(f"\n===== started {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        out.flush()
        proc = subprocess.Popen(
            cmd, cwd=Path.cwd(), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,           # own process group: survives the terminal closing
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    PID_FILE.write_text(json.dumps({"pid": proc.pid, "started": time.time()}))
    time.sleep(1.5)
    if proc.poll() is not None:              # died immediately: show why
        PID_FILE.unlink(missing_ok=True)
        raise RuntimeError("The bot exited right away:\n" + "\n".join(tail(8)))
    return status()


def stop(timeout: float = 6.0) -> bool:
    st = status()
    if not st["running"]:
        return False
    pid = st["pid"]
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline and _alive(pid):
        time.sleep(0.2)
    if _alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    PID_FILE.unlink(missing_ok=True)
    with OUT_FILE.open("a") as out:
        out.write(f"===== stopped {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    return True


def tail(n: int = 15) -> list[str]:
    if not OUT_FILE.exists():
        return []
    with OUT_FILE.open(errors="replace") as f:
        return [line.rstrip("\n") for line in deque(f, maxlen=n)]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Make sure the PID wasn't recycled by some unrelated program.
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except OSError:
        return True
    return "resybot" in cmd
