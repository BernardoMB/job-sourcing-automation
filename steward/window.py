"""The Job Sourcer window: the dashboard in its own window, with its own taskbar icon.

An Edge "--app" window takes its taskbar icon from the page's 16 px favicon, which Windows then
stretches (at 125% scaling the taskbar wants 40 px), so it looks blurred. This window is a
WebView2 window (the same Edge engine) owned by Job Sourcer: its icon is the multi-size .ico in
assets/logo, so Windows picks the size it needs and it stays sharp, in the taskbar and the title
bar. It has its own taskbar identity (APP_ID), shared with the desktop shortcut.

One window at a time: starting it again brings the open one to the front. Closing it only closes
the window; the background server keeps watching, as before.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from steward import ASSETS

APP_ID = "JobSourcer.Dashboard"
TITLE = "Job Sourcer"
ICON = ASSETS / "logo" / "windows" / "icon-256x256px.ico"
_MUTEX = "Local\\JobSourcer.Dashboard.Window"


def available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import webview  # noqa: F401
    except Exception:  # noqa: BLE001 - not installed, or no WebView2: the Edge app window is used instead
        return False
    return True


def _bring_to_front() -> bool:
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if user32.IsWindowVisible(hwnd) and buf.value.endswith(TITLE) and cls.value.startswith("WindowsForms"):
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    for hwnd in found:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    return bool(found)


def _own_windows() -> list[int]:
    user32 = ctypes.windll.user32
    pid = ctypes.windll.kernel32.GetCurrentProcessId()
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if owner.value == pid and user32.IsWindowVisible(hwnd) and cls.value.startswith("WindowsForms"):
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


class _Api:
    """Called by the page: keeps the window title (with its "(3)" count) and the title bar's light/dark
    look in step with the page's."""

    def __init__(self) -> None:
        self._window = None  # private: pywebview exposes (and walks) every public attribute to the page

    def set_title(self, title: str) -> None:
        if self._window is not None and title:
            self._window.set_title(str(title)[:200])

    def set_dark(self, dark: bool) -> None:
        value = ctypes.c_int(1 if dark else 0)
        for hwnd in _own_windows():
            # DWMWA_USE_IMMERSIVE_DARK_MODE, then redraw the frame so the title bar changes right away.
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20, ctypes.byref(value),
                                                       ctypes.sizeof(value))
            ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0, 0x37)  # SWP_FRAMECHANGED etc.


def run(url: str, storage: Path) -> None:
    """Open the window (blocking until it is closed)."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, _MUTEX)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS: one is open already
        _bring_to_front()
        return
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    import webview

    storage.mkdir(parents=True, exist_ok=True)
    api = _Api()
    api._window = webview.create_window(TITLE, url, js_api=api, width=1320, height=900, min_size=(420, 500))
    # private_mode off + storage_path: the page's remembered tab and table filters survive restarts.
    webview.start(icon=str(ICON), private_mode=False, storage_path=str(storage))
