"""Install whatsapp-mcp-ts and link it to your WhatsApp account (once), as a WhatsApp "linked device".

whatsapp-mcp-ts (github.com/jlucaso1/whatsapp-mcp-ts) is the upstream WhatsApp MCP server Job Sourcer reads
and replies through. It keeps its login in auth_info/ and its messages in data/whatsapp.db, both inside its
own folder on this computer. Out of the box it opens its login QR code on quickchart.io, a public website;
Job Sourcer patches that line out and shows the QR code here instead. It also fixes a bug that stores the text
"undefined" as a chat's last-message time (which breaks "most recently active first"), and repairs the times
already stored. Nothing here reads the login files or the messages' text.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from steward.config import Config

REPO = "https://github.com/jlucaso1/whatsapp-mcp-ts"
DEFAULT_DIR = Path.home() / ".whatsapp-mcp-ts"
_QR_LINE = "await open(`https://quickchart.io/qr?text=${encodeURIComponent(qr)}`);"
_PATCHED = "// Job Sourcer patch: the QR code is shown locally, never sent to a website."
# storeChat() writes String(undefined) when an update has no time; == null also catches undefined.
_TIME_LINE = "          : chat.last_message_time === null\n"
_TIME_FIXED = "          : chat.last_message_time == null // Job Sourcer patch: no \"undefined\" times\n"


def main_file(folder: Path) -> Path:
    return folder / "src" / "main.ts"


def installed(folder: Path) -> bool:
    return main_file(folder).exists() and (folder / "node_modules").is_dir()


def linked(folder: Path) -> bool:
    return (folder / "auth_info" / "creds.json").exists()


def patched(folder: Path) -> bool | None:
    """Whether the QR code stays on this computer and chat times are stored correctly (None when the files are
    not there to tell)."""
    source, database = folder / "src" / "whatsapp.ts", folder / "src" / "database.ts"
    if not source.exists() or not database.exists():
        return None
    text = source.read_text(encoding="utf-8")
    return (_PATCHED in text or "quickchart.io" not in text) and _TIME_LINE not in database.read_text(encoding="utf-8").replace("\r\n", "\n")


def folder_of(cfg: Config) -> Path | None:
    up = cfg.whatsapp.upstream
    main = next((a for a in (up.args if up else []) if a.replace("\\", "/").endswith("src/main.ts")), "")
    return Path(main).resolve().parent.parent if main else None


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    exe = shutil.which(cmd[0]) or cmd[0]
    res = subprocess.run([exe, *cmd[1:]], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {(res.stderr or res.stdout)[-800:]}")


def install(folder: Path = DEFAULT_DIR) -> None:
    """Clone and install whatsapp-mcp-ts (needs git and Node.js 23.10 or newer), then patch the QR line."""
    if not main_file(folder).exists():
        _run(["git", "clone", "--depth", "1", REPO, str(folder)])
    if not (folder / "node_modules").is_dir():
        _run(["npm", "install", "--no-fund", "--no-audit"], cwd=folder)
    patch(folder)


def patch(folder: Path) -> None:
    source = folder / "src" / "whatsapp.ts"
    text = source.read_text(encoding="utf-8")
    if _QR_LINE in text:
        source.write_text(text.replace(_QR_LINE, _PATCHED), encoding="utf-8")
    if _PATCHED not in source.read_text(encoding="utf-8") and "quickchart.io" in source.read_text(encoding="utf-8"):
        raise RuntimeError(f"Could not patch {source}: it no longer has the expected quickchart.io line. Remove "
                           "the line that opens quickchart.io yourself before linking.")
    database = folder / "src" / "database.ts"
    text = database.read_text(encoding="utf-8").replace("\r\n", "\n")
    if _TIME_LINE in text:
        database.write_text(text.replace(_TIME_LINE, _TIME_FIXED), encoding="utf-8")
    repair_chat_times(folder)


def repair_chat_times(folder: Path) -> int:
    """Each chat's last-message time from its newest stored message (it may hold "undefined" or a stale time).
    Run while the server is stopped. Returns how many chats changed."""
    store = folder / "data" / "whatsapp.db"
    if not store.exists():
        return 0
    with sqlite3.connect(store, timeout=10) as db:
        cur = db.execute(
            "UPDATE chats SET last_message_time = (SELECT MAX(m.timestamp) FROM messages m WHERE m.chat_jid = chats.jid) "
            "WHERE EXISTS (SELECT 1 FROM messages m WHERE m.chat_jid = chats.jid) AND (last_message_time IS NULL "
            "OR last_message_time = 'undefined' OR last_message_time < (SELECT MAX(m.timestamp) FROM messages m "
            "WHERE m.chat_jid = chats.jid))")
        cleared = db.execute("UPDATE chats SET last_message_time = NULL WHERE last_message_time = 'undefined'")
        return cur.rowcount + cleared.rowcount


def configure(cfg: Config, folder: Path) -> Config:
    from steward.setup import update_config

    return update_config(cfg, {"whatsapp.backend": "upstream", "whatsapp.upstream": {
        "command": "node", "args": [str(main_file(folder))], "env": {}, "url": "", "token_env": "", "tools": {}}})


def link(folder: Path, show_qr: Callable[[str], None], say: Callable[[str], None],
         timeout: float = 300, sync_seconds: float = 45) -> bool:
    """Start the server, show each QR code it receives, and wait until WhatsApp says the device is linked.

    It then stays connected a little longer so the first history sync lands in its database."""
    log = folder / "wa-logs.txt"
    offset = log.stat().st_size if log.exists() else 0
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    # stdin stays open: the server is an MCP stdio server and exits when its input closes.
    proc = subprocess.Popen([shutil.which("node") or "node", str(main_file(folder))], cwd=folder,
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=flags)
    opened_at = 0.0
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"whatsapp-mcp-ts exited (code {proc.returncode}); see {log}")
            for entry in _new_entries(log, offset):
                offset = entry.pop("_offset")
                if entry.get("qrCodeData"):
                    show_qr(entry["qrCodeData"])
                elif "Connection opened" in str(entry.get("msg", "")) and not opened_at:
                    opened_at = time.time()
                    say("Linked. Receiving your recent chats...")
                elif "Logged Out" in str(entry.get("msg", "")):
                    raise RuntimeError("WhatsApp logged this device out. Delete the auth_info folder in "
                                       f"{folder} and run this again.")
            if opened_at and time.time() - opened_at >= sync_seconds:
                return True
            time.sleep(0.5)
        return bool(opened_at)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _new_entries(log: Path, offset: int) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    out = []
    with open(log, "rb") as f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # a line still being written
            offset += len(raw)
            try:
                entry = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                entry = {}
            out.append({**(entry if isinstance(entry, dict) else {}), "_offset": offset})
    return out
