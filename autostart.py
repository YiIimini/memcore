#!/usr/bin/env python3
"""launchd helpers for the two memcore background jobs.

Naming rule (user requirement): every background item gets a human-readable
label + matching plist filename, never a cryptic one. Current set:

    MemoryPanel      → ~/Library/LaunchAgents/MemoryPanel.plist      (常驻面板服务)
    MemoryMaintain   → ~/Library/LaunchAgents/MemoryMaintain.plist   (每 30 分钟维护)

Legacy labels (com.memcore.*) are removed on enable().
"""
from __future__ import annotations

import os
import plistlib
import subprocess

AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
MEM = os.path.expanduser("~/.dsh/memory-core/mem")
PY = "/usr/bin/python3"
HOME = os.path.expanduser("~")
LOG_DIR = os.path.expanduser("~/.dsh/memory")

PANEL_LABEL = "MemoryPanel"
MAINTAIN_LABEL = "MemoryMaintain"
PANEL_PLIST = os.path.join(AGENTS_DIR, PANEL_LABEL + ".plist")
MAINTAIN_PLIST = os.path.join(AGENTS_DIR, MAINTAIN_LABEL + ".plist")
LEGACY = ("com.memcore.ui", "com.memcore.maintain")

PATH = "/Users/x/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _run(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": str(e)})()


def _where(label):
    return "gui/%d/%s" % (os.getuid(), label)


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(payload, fh)


def _load(path, label):
    _run("launchctl", "bootout", _where(label))
    r = _run("launchctl", "bootstrap", "gui/%d" % os.getuid(), path)
    if r.returncode != 0:
        _run("launchctl", "unload", path)
        r = _run("launchctl", "load", "-w", path)
    return r


def _unload(path, label):
    _run("launchctl", "bootout", _where(label))
    _run("launchctl", "unload", path)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def cleanup_legacy() -> list:
    """Remove the old com.memcore.* agents so only the readable ones remain."""
    removed = []
    for lab in LEGACY:
        p = os.path.join(AGENTS_DIR, lab + ".plist")
        if os.path.exists(p):
            _unload(p, lab)
            removed.append(p)
        else:
            _run("launchctl", "bootout", _where(lab))
    return removed


# --------------------------------------------------------------------------
# MemoryPanel — always-on UI
# --------------------------------------------------------------------------

def enable_ui(port: int = 8790, home: str = "") -> dict:
    payload = {
        "Label": PANEL_LABEL,
        "ProgramArguments": [PY, MEM, "serve", "--port", str(int(port))],
        "RunAtLoad": True,
        "KeepAlive": True,
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": os.path.join(LOG_DIR, "MemoryPanel.log"),
        "StandardErrorPath": os.path.join(LOG_DIR, "MemoryPanel.log"),
        "EnvironmentVariables": {"PATH": PATH, "HOME": HOME, **({"MEM_HOME": home} if home else {})},
        "ProcessType": "Interactive",
    }
    _write(PANEL_PLIST, payload)
    r = _load(PANEL_PLIST, PANEL_LABEL)
    return {"enabled": r.returncode == 0, "label": PANEL_LABEL,
            "url": "http://127.0.0.1:%d/" % int(port), "plist": PANEL_PLIST,
            "detail": (r.stderr or r.stdout or "").strip()[:300]}


def disable_ui() -> dict:
    _unload(PANEL_PLIST, PANEL_LABEL)
    return {"enabled": False, "label": PANEL_LABEL}


# --------------------------------------------------------------------------
# MemoryMaintain — periodic ingestion
# --------------------------------------------------------------------------

def enable(interval_minutes: int = 30, home: str = "") -> dict:
    minutes = max(5, int(interval_minutes or 30))
    payload = {
        "Label": MAINTAIN_LABEL,
        "ProgramArguments": [PY, MEM, "maintain"],
        "StartInterval": minutes * 60,
        "RunAtLoad": False,
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": os.path.join(LOG_DIR, "MemoryMaintain.log"),
        "StandardErrorPath": os.path.join(LOG_DIR, "MemoryMaintain.log"),
        "EnvironmentVariables": {"PATH": PATH, "HOME": HOME, **({"MEM_HOME": home} if home else {})},
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
    }
    _write(MAINTAIN_PLIST, payload)
    r = _load(MAINTAIN_PLIST, MAINTAIN_LABEL)
    return {"enabled": r.returncode == 0, "label": MAINTAIN_LABEL,
            "interval_minutes": minutes, "plist": MAINTAIN_PLIST,
            "detail": (r.stderr or r.stdout or "").strip()[:300]}


def disable() -> dict:
    _unload(MAINTAIN_PLIST, MAINTAIN_LABEL)
    return {"enabled": False, "label": MAINTAIN_LABEL}


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def _alive(url: str, timeout: float = 1.5) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _interval(path):
    try:
        with open(path, "rb") as fh:
            return plistlib.load(fh).get("StartInterval")
    except Exception:
        return None


def status() -> dict:
    return {"panel": ui_status(), "maintain": maintain_status()}


def ui_status() -> dict:
    loaded = _run("launchctl", "print", _where(PANEL_LABEL)).returncode == 0
    port = 8790
    try:
        with open(PANEL_PLIST, "rb") as fh:
            args = plistlib.load(fh).get("ProgramArguments") or []
            if "--port" in args:
                port = int(args[args.index("--port") + 1])
    except Exception:
        pass
    alive = _alive("http://127.0.0.1:%d/api/stats" % port)
    return {"label": PANEL_LABEL, "name": "记忆面板", "desc": "常驻的图表/设置界面",
            "enabled": bool(loaded and alive), "loaded": loaded, "alive": alive,
            "port": port, "url": "http://127.0.0.1:%d/" % port, "plist": PANEL_PLIST}


def maintain_status() -> dict:
    loaded = _run("launchctl", "print", _where(MAINTAIN_LABEL)).returncode == 0
    iv = _interval(MAINTAIN_PLIST)
    last = 0
    p = os.path.join(LOG_DIR, "MemoryMaintain.log")
    try:
        last = int(os.path.getmtime(p))
    except OSError:
        pass
    return {"label": MAINTAIN_LABEL, "name": "记忆维护", "desc": "定时入库/建边/清理",
            "enabled": loaded, "loaded": loaded, "interval_seconds": iv,
            "last_log": last, "plist": MAINTAIN_PLIST}


def agents(panel_alive=None) -> list:
    """panel_alive: pass True when called from inside the panel server itself
    (avoids a self-probe that can race its own startup)."""
    p = ui_status()
    if panel_alive is not None:
        p["alive"] = bool(panel_alive)
        p["enabled"] = bool(p["loaded"] and p["alive"])
    return [p, maintain_status()]
