"""Native folder and file choosers, and the user's real Videos/Documents.

On Windows the choosers shell out to PowerShell, which is always present and
needs nothing bundled: a frozen build has no python.exe to re-run and a venv
gives PyInstaller no tcl/tk to collect, so tkinter is the fragile option here,
not the safe one. tkinter stays as the fallback for other platforms.

The chosen path travels back over stdout, which PowerShell encodes in the
console's OEM code page unless told otherwise. That turned every Persian or
Arabic folder name into '????', so the script switches stdout to UTF-8 and
Python decodes it as UTF-8. The start folder goes in through an environment
variable, never into the script text.
"""
from __future__ import annotations

import os
import subprocess
import sys

_PRELUDE = r"""
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
"""

_FOLDER_PS = _PRELUDE + r"""
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Choose a folder for Media Toolkit'
$d.ShowNewFolderButton = $true
try { $d.SelectedPath = $env:MT_PICK_START } catch { }
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($d.SelectedPath)
}
$owner.Dispose()
"""

_FILE_PS = _PRELUDE + r"""
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = 'Choose a file for Media Toolkit'
$d.Filter = $env:MT_PICK_FILTER
$d.CheckFileExists = $true
try {
    if (Test-Path -LiteralPath $env:MT_PICK_START -PathType Container) {
        $d.InitialDirectory = $env:MT_PICK_START
    } else {
        $d.InitialDirectory = Split-Path -LiteralPath $env:MT_PICK_START
    }
} catch { }
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($d.FileName)
}
$owner.Dispose()
"""

_FILTERS = {
    "cookies": "Cookie files (*.txt)|*.txt|All files (*.*)|*.*",
    "": "All files (*.*)|*.*",
}


def _run_ps(script: str, start: str, extra_env: dict | None = None) -> str | None:
    """Run a picker script; returns the picked path, "" when cancelled, or
    None when no PowerShell could run it."""
    env = dict(os.environ)
    env["MT_PICK_START"] = start
    env.update(extra_env or {})
    for exe in ("powershell.exe", "pwsh.exe"):
        try:
            proc = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
                capture_output=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=600, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return ""           # the dialog sat open too long; never open a second one
        picked = (proc.stdout or "").strip().lstrip("﻿")
        if picked or proc.returncode == 0:
            return picked
    return None


def choose(start: str = "") -> str:
    """Return the chosen directory, or "" if the user cancelled."""
    start = start or os.path.expanduser("~")
    if os.name == "nt":
        picked = _run_ps(_FOLDER_PS, start) or ""
        return picked if picked and os.path.isdir(picked) else ""

    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        picked = filedialog.askdirectory(initialdir=start, title="Choose a folder")
        root.destroy()
        return picked or ""
    except Exception:
        return ""


def choose_file(start: str = "", kind: str = "") -> str:
    """Return the chosen file, or "" if the user cancelled."""
    start = start or os.path.expanduser("~")
    if os.name == "nt":
        picked = _run_ps(_FILE_PS, start, {"MT_PICK_FILTER": _FILTERS.get(kind, _FILTERS[""])})
        return picked if picked and os.path.isfile(picked) else ""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        picked = filedialog.askopenfilename(initialdir=start, title="Choose a file")
        root.destroy()
        return picked or ""
    except Exception:
        return ""


# Known folder ids (KNOWNFOLDERID) for the places people expect files.
_KNOWN = {
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
}


def known_folder(name: str) -> str:
    """Where Windows really keeps Videos or Documents. OneDrive backup and
    folder redirection move them away from %USERPROFILE%, and a folder
    created at the old spot is one the user never finds in Explorer.
    Returns "" when unknown (not Windows, or the lookup failed)."""
    guid = _KNOWN.get(name)
    if os.name != "nt" or not guid:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        fid = GUID()
        if ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(guid), ctypes.byref(fid)) != 0:
            return ""
        out = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD,
                                                 wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        if shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out)) != 0:
            return ""
        try:
            return out.value or ""
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        return ""


if __name__ == "__main__":
    sys.stdout.write(choose(sys.argv[1] if len(sys.argv) > 1 else ""))
