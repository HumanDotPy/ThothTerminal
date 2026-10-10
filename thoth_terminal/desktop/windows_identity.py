"""Windows taskbar identity for the desktop shell and its launch shortcuts."""
from __future__ import annotations

import os
from pathlib import Path

WINDOWS_APP_ID = "Thoth.Terminal.Desktop"


def set_windows_process_identity() -> None:
    """Call before creating any windows so the taskbar doesn't identify Python."""
    if os.name != "nt":
        return
    from win32com.shell import shell

    shell.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)


def set_windows_shortcut_identity(path: str | Path) -> None:
    """Preserve an existing shortcut's launch/icon settings and assign our ID."""
    if os.name != "nt":
        return
    import pythoncom
    from win32com import storagecon
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    shortcut_path = str(Path(path).resolve(strict=True))
    link = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink,
    )
    file = link.QueryInterface(pythoncom.IID_IPersistFile)
    file.Load(shortcut_path, storagecon.STGM_READWRITE)
    properties = link.QueryInterface(propsys.IID_IPropertyStore)
    properties.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(WINDOWS_APP_ID))
    properties.Commit()
    file.Save(shortcut_path, True)
