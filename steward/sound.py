"""The new-event chime.

Played on this computer by the background process (not by the dashboard page), so
you hear it even when the window is closed or minimized, and a browser's autoplay
rules can never swallow it. Windows only: it uses the built-in MCI player, which
handles MP3 with no extra libraries.
"""

from __future__ import annotations

import itertools
import logging
import sys
import threading
from pathlib import Path

from steward import ASSETS
from steward.config import Config

log = logging.getLogger("steward.sound")
CHIME = ASSETS / "Tethys Nexus 5.mp3"
_aliases = itertools.count(1)


def chime_file(cfg: Config) -> Path:
    return cfg.resolve(cfg.notifications.sound_file) if cfg.notifications.sound_file else CHIME


def _play_windows(path: Path, alias: str, opened: threading.Event, result: dict[str, bool]) -> None:
    import ctypes

    mci = ctypes.windll.winmm.mciSendStringW
    # Open, play and close on this one thread: an MCI alias belongs to the thread that opened it.
    err = mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
    result["ok"] = err == 0
    opened.set()
    if err:
        log.warning("could not play %s (MCI error %s)", path, err)
        return
    try:
        mci(f"play {alias} wait", None, 0, None)
    finally:
        mci(f"close {alias}", None, 0, None)


def play(path: Path, wait: float = 0.0) -> bool:
    """Play `path` without blocking. With `wait`, wait up to that many seconds to report whether it opened."""
    if sys.platform != "win32" or not path.is_file():
        return False
    opened, result = threading.Event(), {"ok": False}
    threading.Thread(target=_play_windows, args=(path, f"jobsourcer_chime_{next(_aliases)}", opened, result),
                     daemon=True).start()
    if not wait:
        return True
    opened.wait(wait)
    return result["ok"]


def new_events(cfg: Config, count: int) -> bool:
    """One chime per check that brought new events, if the sound is on."""
    if count <= 0 or not cfg.notifications.sound:
        return False
    return play(chime_file(cfg))
