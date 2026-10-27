#!/usr/bin/env python3
"""Add BenchPanel to the desktop's app menu, or take it out again.

    benchpanel --install-launcher
    benchpanel --uninstall-launcher

The launcher opens a terminal window running BenchPanel, and the browser on it.
Closing that window stops BenchPanel the safe way (outputs off, front panels
handed back) -- the same as Ctrl-C. Clicking the launcher while BenchPanel is
already running just opens the browser on it.

  Linux    a freedesktop .desktop entry + icon in ~/.local/share. GNOME may only
           show a NEW entry after you log out and back in.
  Windows  a Start-menu shortcut, made through PowerShell (no extra packages).
           NOT YET TESTED on a Windows machine.
  macOS    not supported yet -- run `benchpanel` from Terminal.

Nothing here needs admin rights: everything goes in the user's own folders.
"""
import os
import shlex
import shutil
import subprocess
import sys

APP_ID = "benchpanel"
NAME = "BenchPanel"
COMMENT = "Plug-in dashboard for bench test instruments"

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="10" fill="#16213e"/>
  <rect x="7" y="9" width="50" height="22" rx="3" fill="#04121a" stroke="#b8860b" stroke-width="2"/>
  <text x="32" y="26" font-family="monospace" font-size="15" font-weight="bold"
        fill="#3fd2ff" text-anchor="middle">3.29</text>
  <circle cx="16" cy="46" r="7" fill="none" stroke="#f0c040" stroke-width="3"/>
  <line x1="16" y1="46" x2="16" y2="40" stroke="#f0c040" stroke-width="3" stroke-linecap="round"/>
  <circle cx="34" cy="46" r="7" fill="none" stroke="#f0c040" stroke-width="3"/>
  <line x1="34" y1="46" x2="39" y2="42" stroke="#f0c040" stroke-width="3" stroke-linecap="round"/>
  <circle cx="52" cy="46" r="5" fill="#3fb950"/>
</svg>
"""


class LauncherError(Exception):
    pass


def command():
    """The exact command line the launcher should run -- the one running now.

    Installed: the `benchpanel` script. From a checkout: python3 /path/bench_panel.py,
    which keeps that copy's portable data folder.
    """
    argv0 = os.path.abspath(sys.argv[0])
    if argv0.endswith(".py"):
        return [sys.executable, argv0]
    found = shutil.which(os.path.basename(sys.argv[0])) or argv0
    return [os.path.abspath(found)]


def _xdg_data():
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def _linux_paths():
    d = _xdg_data()
    return (os.path.join(d, "applications", f"{APP_ID}.desktop"),
            os.path.join(d, "icons", "hicolor", "scalable", "apps", f"{APP_ID}.svg"))


def install():
    if sys.platform.startswith("linux"):
        return _install_linux()
    if sys.platform.startswith("win"):
        return _install_windows()
    raise LauncherError(f"no launcher for {sys.platform} yet -- run `benchpanel` from a terminal")


def uninstall():
    if sys.platform.startswith("linux"):
        gone = []
        for p in _linux_paths():
            if os.path.exists(p):
                os.remove(p)
                gone.append(p)
        _refresh_linux()
        return "removed: " + ", ".join(gone) if gone else "no launcher was installed"
    if sys.platform.startswith("win"):
        p = _windows_lnk()
        if os.path.exists(p):
            os.remove(p)
            return f"removed: {p}"
        return "no launcher was installed"
    raise LauncherError(f"no launcher for {sys.platform}")


def _install_linux():
    desktop, icon = _linux_paths()
    os.makedirs(os.path.dirname(desktop), exist_ok=True)
    os.makedirs(os.path.dirname(icon), exist_ok=True)
    with open(icon, "w", encoding="utf-8") as fh:
        fh.write(ICON_SVG)
    # Exec: freedesktop spec quoting. Terminal=true gives the operator a window
    # whose closing is the stop button (SIGHUP -> safe shutdown).
    exec_line = " ".join(shlex.quote(c) for c in command())
    entry = "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        f"Name={NAME}",
        f"Comment={COMMENT}",
        f"Exec={exec_line}",
        f"Icon={APP_ID}",
        "Terminal=true",
        "Categories=Science;Electronics;Engineering;",
        "Keywords=instrument;GPIB;SCPI;oscilloscope;multimeter;power supply;bench;",
        "StartupNotify=false",
        "",
    ])
    with open(desktop, "w", encoding="utf-8") as fh:
        fh.write(entry)
    os.chmod(desktop, 0o755)
    problems = ""
    if shutil.which("desktop-file-validate"):
        r = subprocess.run(["desktop-file-validate", desktop], capture_output=True, text=True)
        problems = (r.stdout + r.stderr).strip()
    _refresh_linux()
    return (f"installed: {desktop}\n  runs: {exec_line}\n  icon: {icon}\n"
            + (f"  desktop-file-validate: {problems}\n" if problems else "  desktop-file-validate: OK\n")
            + "  GNOME may only show a new entry after you log out and back in.")


def _refresh_linux():
    d = _xdg_data()
    for cmd in (["update-desktop-database", os.path.join(d, "applications")],
                ["gtk-update-icon-cache", "-f", "-t", os.path.join(d, "icons", "hicolor")]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, capture_output=True)


def _windows_lnk():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", f"{NAME}.lnk")


def _install_windows():
    """UNTESTED on Windows. Uses the WScript.Shell COM object through PowerShell."""
    lnk = _windows_lnk()
    cmd = command()
    target, args = cmd[0], subprocess.list2cmdline(cmd[1:])

    def ps(s):
        return "'" + s.replace("'", "''") + "'"
    script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({ps(lnk)});"
              f"$s.TargetPath={ps(target)};$s.Arguments={ps(args)};"
              f"$s.Description={ps(COMMENT)};$s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True)
    if r.returncode != 0:
        raise LauncherError(f"PowerShell could not create the shortcut: {r.stderr.strip()}")
    return f"installed: {lnk}\n  runs: {target} {args}"
