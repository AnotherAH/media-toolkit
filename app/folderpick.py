"""Native folder chooser.

On Windows this shells out to PowerShell, which is always present and needs
nothing bundled -- a frozen build has no python.exe to re-run and a venv gives
PyInstaller no tcl/tk to collect, so tkinter is the fragile option here, not the
safe one. tkinter stays as the fallback for other platforms.
"""
from __future__ import annotations

import os
import subprocess
import sys

_PS = r"""
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Choose a folder for Media Toolkit'
$d.ShowNewFolderButton = $true
try {{ $d.SelectedPath = '{start}' }} catch {{ }}
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {{
    [Console]::Out.Write($d.SelectedPath)
}}
$owner.Dispose()
"""


def choose(start: str = "") -> str:
    """Return the chosen directory, or "" if the user cancelled."""
    start = (start or os.path.expanduser("~")).replace("'", "''")
    if os.name == "nt":
        for exe in ("powershell.exe", "pwsh.exe"):
            try:
                proc = subprocess.run(
                    [exe, "-NoProfile", "-NonInteractive", "-STA", "-Command",
                     _PS.format(start=start)],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            picked = (proc.stdout or "").strip()
            if picked or proc.returncode == 0:
                return picked
        return ""

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


if __name__ == "__main__":
    sys.stdout.write(choose(sys.argv[1] if len(sys.argv) > 1 else ""))
