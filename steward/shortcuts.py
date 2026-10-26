"""Desktop and Start menu shortcuts for Job Sourcer (Windows).

They start the app with pythonw.exe (no console window at all), show the multi-size .ico (sharp at
every size Windows draws it), and carry the window's taskbar identity (window.APP_ID). A pinned
shortcut and the running window are then one taskbar button with one icon.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from steward.config import Config
from steward.window import APP_ID, ICON

NAME = "Job Sourcer.lnk"


def locations() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    desktop = home / "Desktop"
    try:  # a Desktop redirected to OneDrive or elsewhere
        from win32com.shell import shell, shellcon

        desktop = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0))
    except Exception:  # noqa: BLE001
        pass
    programs = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return [desktop / NAME, programs / NAME]


def create(cfg: Config) -> list[Path]:
    """Create (or refresh) the shortcuts. Returns where they were written."""
    if sys.platform != "win32":
        raise RuntimeError("Shortcuts are only created on Windows")
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.exists():
        python = Path(sys.executable)
    project = Path(__file__).resolve().parent.parent
    written = []
    for path in locations():
        path.parent.mkdir(parents=True, exist_ok=True)
        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                          shell.IID_IShellLink)
        link.SetPath(str(python))
        link.SetArguments(f'-m steward -c "{cfg.config_path}" app')
        link.SetWorkingDirectory(str(project))
        link.SetIconLocation(str(ICON), 0)
        link.SetDescription("Job Sourcer: your recruiter-facing job search assistant")
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
        store.Commit()
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 0)
        written.append(path)
    return written
