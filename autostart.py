#!/usr/bin/env python3
"""launchd helper: keep `mem maintain` running in the background (macOS)."""
from __future__ import annotations

import os
import plistlib
import subprocess

PLIST = os.path.expanduser("~/Library/LaunchAgents/com.memcore.maintain.plist")
LABEL = "com.memcore.maintain"
UI_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.memcore.ui.plist")
UI_LABEL = "com.memcore.ui"
UI_PORT = 8787
MEM = os.path.expanduser("~/.dsh/memory-core/mem")
PY = "/usr/bin/python3"
LOG = os.path.expanduser("~/.dsh/memory/maintain.log")


def _run(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": str(e)})()


def enable(interval_minutes: int = 30, home: str = "") -> dict:
    minutes = max(5, int(interval_minutes or 30))
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    env = {"PATH": "/Users/x/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": os.path.expanduser("~")}
    if home:
        env["MEM_HOME"] = home
    payload = {
        "Label": LABEL,
        "ProgramArguments": [PY, MEM, "maintain"],
        "StartInterval": minutes * 60,
        "RunAtLoad": False,
        "StandardOutPath": LOG,
        "StandardErrorPath": LOG,
        "EnvironmentVariables": env,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
    }
    with open(PLIST, "wb") as fh:
        plistlib.dump(payload, fh)
    _run("launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL))
    r = _run("launchctl", "bootstrap", "gui/%d" % os.getuid(), PLIST)
    if r.returncode != 0:
        _run("launchctl", "unload", PLIST)
        r = _run("launchctl", "load", "-w", PLIST)
    return {"enabled": r.returncode == 0, "interval_minutes": minutes,
            "plist": PLIST, "detail": (r.stderr or r.stdout or "").strip()[:300]}


def disable() -> dict:
    _run("launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL))
    _run("launchctl", "unload", PLIST)
    try:
        os.remove(PLIST)
    except FileNotFoundError:
        pass
    return {"enabled": False, "plist": PLIST}


def status() -> dict:
    exists = os.path.exists(PLIST)
    interval = None
    if exists:
        try:
            with open(PLIST, "rb") as fh:
                interval = plistlib.load(fh).get("StartInterval")
        except Exception:
            pass
    r = _run("launchctl", "print", "gui/%d/%s" % (os.getuid(), LABEL))
    loaded = r.returncode == 0
    last = 0
    try:
        last = int(os.path.getmtime(LOG))
    except OSError:
        pass
    return {"enabled": bool(exists and loaded), "installed": exists, "loaded": loaded,
            "interval_seconds": interval, "last_log": last, "plist": PLIST}


# --------------------------------------------------------------------------
# always-on panel server (so the UI is a bookmark, never a command)
# --------------------------------------------------------------------------

def enable_ui(port: int = UI_PORT, home: str = "") -> dict:
    os.makedirs(os.path.dirname(UI_PLIST), exist_ok=True)
    payload = {
        "Label": UI_LABEL,
        "ProgramArguments": [PY, MEM, "serve", "--port", str(int(port))],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": os.path.expanduser("~/.dsh/memory/ui-server.log"),
        "StandardErrorPath": os.path.expanduser("~/.dsh/memory/ui-server.log"),
        "EnvironmentVariables": {
            "PATH": "/Users/x/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": os.path.expanduser("~"),
            **({"MEM_HOME": home} if home else {}),
        },
        "ProcessType": "Interactive",
    }
    with open(UI_PLIST, "wb") as fh:
        plistlib.dump(payload, fh)
    _run("launchctl", "bootout", "gui/%d/%s" % (os.getuid(), UI_LABEL))
    r = _run("launchctl", "bootstrap", "gui/%d" % os.getuid(), UI_PLIST)
    if r.returncode != 0:
        _run("launchctl", "unload", UI_PLIST)
        r = _run("launchctl", "load", "-w", UI_PLIST)
    return {"enabled": r.returncode == 0, "url": "http://127.0.0.1:%d/" % int(port),
            "plist": UI_PLIST, "detail": (r.stderr or r.stdout or "").strip()[:300]}


def disable_ui() -> dict:
    _run("launchctl", "bootout", "gui/%d/%s" % (os.getuid(), UI_LABEL))
    _run("launchctl", "unload", UI_PLIST)
    try:
        os.remove(UI_PLIST)
    except FileNotFoundError:
        pass
    return {"enabled": False}


def ui_status() -> dict:
    r = _run("launchctl", "print", "gui/%d/%s" % (os.getuid(), UI_LABEL))
    loaded = r.returncode == 0
    port = UI_PORT
    try:
        with open(UI_PLIST, "rb") as fh:
            args = plistlib.load(fh).get("ProgramArguments") or []
            if "--port" in args:
                port = int(args[args.index("--port") + 1])
    except Exception:
        pass
    alive = False
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/api/stats" % port, timeout=1.5) as resp:
            alive = resp.status == 200
    except Exception:
        alive = False
    return {"enabled": bool(loaded and alive), "loaded": loaded, "alive": alive,
            "port": port, "url": "http://127.0.0.1:%d/" % port}
