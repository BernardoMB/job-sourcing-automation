"""Keeps the LinkedIn toolkit's bridge (`lit serve --http`) running in the background.

The toolkit's browser extension (Edge or Chrome) connects to a long-running local bridge;
a toolkit process started per check would be gone before the extension reconnects. So
Steward starts the bridge once, restarts it if it stops, and talks to it over HTTP on
127.0.0.1. Nothing here reads the toolkit's files or its pairing token.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse

from steward.config import Config

_process: subprocess.Popen | None = None
_last_start = 0.0
RESTART_BACKOFF_SECONDS = 60


def _endpoint(cfg: Config) -> tuple[str, int] | None:
    li = cfg.linkedin
    if li.backend != "upstream" or li.upstream is None or not li.upstream.url:
        return None
    u = urlparse(li.upstream.url)
    if u.hostname not in ("127.0.0.1", "localhost", "::1"):
        return None  # a bridge on another machine is not Steward's to run
    return u.hostname, u.port or 80


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def ensure_running(cfg: Config, wait_seconds: float = 20) -> dict[str, Any]:
    """Start the bridge if LinkedIn uses a local one and nothing is listening yet."""
    global _process, _last_start
    target = _endpoint(cfg)
    if target is None or not cfg.linkedin.manage_bridge:
        return {"managed": False}
    host, port = target
    if _listening(host, port):
        return {"managed": True, "running": True, "started": False}
    if _process is not None and _process.poll() is None:
        return {"managed": True, "running": False, "started": False, "note": "bridge is still starting"}
    if time.time() - _last_start < RESTART_BACKOFF_SECONDS:
        return {"managed": True, "running": False, "started": False, "note": "waiting before restarting the bridge"}
    cmd = list(cfg.linkedin.bridge_command)
    exe = shutil.which(cmd[0]) or cmd[0]
    log_dir = cfg.data_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    _last_start = time.time()
    with open(log_dir / "linkedin-bridge.log", "a", encoding="utf-8") as err:
        # stdout is discarded: the toolkit may print its pairing token there.
        _process = subprocess.Popen([exe, *cmd[1:]], stdout=subprocess.DEVNULL, stderr=err,
                                    stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _listening(host, port):
            return {"managed": True, "running": True, "started": True}
        if _process.poll() is not None:
            return {"managed": True, "running": False, "started": True,
                    "note": f"the bridge exited (code {_process.returncode}); see data/logs/linkedin-bridge.log"}
        time.sleep(0.5)
    return {"managed": True, "running": False, "started": True, "note": "the bridge is taking long to start"}


def status(cfg: Config) -> dict[str, Any]:
    target = _endpoint(cfg)
    if target is None:
        return {"local": False}
    return {"local": True, "running": _listening(*target), "managed": cfg.linkedin.manage_bridge,
            "endpoint": f"{target[0]}:{target[1]}"}
