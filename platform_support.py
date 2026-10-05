"""
The few things that genuinely differ between the Windows and Linux builds:
where config lives, how "start at login" is registered, how the headless agent
is installed as a service, and the Wayland workaround the HUD needs.

Everything else (the HUD, the monitor, the remote poller) is one shared
codebase — the per-OS differences in runner discovery and in stopping Ollama
live next to the code they affect, in model_state.py and ollama_control.py.

Autostart, per OS:
- Windows: a value under HKCU\\...\\CurrentVersion\\Run pointing at the venv's
  pythonw.exe (no console window). No admin rights, no COM, no shortcut files.
- Linux: an XDG autostart entry in ~/.config/autostart for the overlay, and a
  systemd *user* service for the agent (so it runs without a desktop session
  once lingering is enabled — which the installer does).
"""
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "local-ai-overlay-pro"
APP_NAME = "Local AI Overlay Pro"
APP_VERSION = "2.0.0"

HERE = Path(__file__).resolve().parent

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUES = {"overlay": "LocalAIOverlayPro", "agent": "LocalAIOverlayProAgent"}
_SCRIPTS = {"overlay": "app.py", "agent": "agent.py"}
AGENT_UNIT = "local-ai-overlay-agent.service"


def is_windows() -> bool:
    return os.name == "nt"


def is_linux() -> bool:
    return platform.system() == "Linux"


def config_dir() -> Path:
    if is_windows():
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        path = base / "LocalAIOverlayPro"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        path = base / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def gui_python() -> str:
    """Interpreter for launching the GUI with no console: pythonw.exe on Windows."""
    exe = Path(sys.executable)
    if is_windows():
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def _launch_command(kind: str, extra: list = None) -> list:
    return [gui_python(), str(HERE / _SCRIPTS[kind])] + list(extra or [])


def prefer_xcb_on_wayland() -> None:
    """Run the HUD through XWayland on Wayland sessions.

    Native Wayland ignores both QWidget.move() and WindowStaysOnTopHint, so a
    frameless overlay can't be pinned to a corner or kept above other windows.
    XWayland honours both. Only applied when the user hasn't chosen a platform
    and an X display is actually available, so a pure-Wayland box still starts.
    """
    if not is_linux() or os.environ.get("QT_QPA_PLATFORM"):
        return
    if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"


# --------------------------------------------------------------------------- #
# Autostart
# --------------------------------------------------------------------------- #

def _xdg_autostart_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / f"{APP_ID}.desktop"


def _user_unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / AGENT_UNIT


def autostart_enabled(kind: str = "overlay") -> bool:
    if is_windows():
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, _RUN_VALUES[kind])
            return True
        except OSError:
            return False
    if kind == "overlay":
        return _xdg_autostart_path().exists()
    return _user_unit_path().exists()


def set_autostart(enabled: bool, kind: str = "overlay", extra_args: list = None) -> tuple:
    """Register/unregister start-at-login. Returns (ok, message)."""
    try:
        if is_windows():
            return _set_autostart_windows(enabled, kind, extra_args)
        if kind == "overlay":
            return _set_autostart_xdg(enabled)
        return set_agent_user_service(enabled, extra_args)
    except Exception as exc:  # surfaced in the settings window / installer output
        return False, f"Couldn't change autostart: {exc}"


def _set_autostart_windows(enabled: bool, kind: str, extra_args) -> tuple:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
        if enabled:
            command = subprocess.list2cmdline(_launch_command(kind, extra_args))
            winreg.SetValueEx(key, _RUN_VALUES[kind], 0, winreg.REG_SZ, command)
            return True, "Starts at login"
        try:
            winreg.DeleteValue(key, _RUN_VALUES[kind])
        except FileNotFoundError:
            pass
        return True, "Won't start at login"


def _set_autostart_xdg(enabled: bool) -> tuple:
    path = _xdg_autostart_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return True, "Won't start at login"
    path.parent.mkdir(parents=True, exist_ok=True)
    exec_line = " ".join(shlex.quote(part) for part in _launch_command("overlay"))
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Ollama status HUD for this and other machines\n"
        f"Exec={exec_line}\n"
        "Icon=utilities-system-monitor\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=5\n",
        encoding="utf-8",
    )
    return True, "Starts at login"


def set_agent_user_service(enabled: bool, extra_args: list = None) -> tuple:
    """Install/remove the agent as a systemd --user service (Linux)."""
    if shutil.which("systemctl") is None:
        return False, "systemd not found — start agent.py some other way (cron @reboot, etc.)"
    path = _user_unit_path()
    if not enabled:
        subprocess.run(["systemctl", "--user", "disable", "--now", AGENT_UNIT],
                       capture_output=True, text=True)
        path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return True, "Agent service removed"

    path.parent.mkdir(parents=True, exist_ok=True)
    exec_line = " ".join(shlex.quote(p) for p in
                         [sys.executable, str(HERE / "agent.py")] + list(extra_args or []))
    path.write_text(
        "[Unit]\n"
        f"Description={APP_NAME} agent (shares this machine's Ollama status)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={exec_line}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    result = subprocess.run(["systemctl", "--user", "enable", "--now", AGENT_UNIT],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return False, f"systemctl --user enable failed: {result.stderr.strip()}"
    return True, f"Agent running as user service {AGENT_UNIT}"


def open_path(path: Path) -> None:
    """Open a file or folder in the desktop's default handler."""
    try:
        if is_windows():
            os.startfile(str(path))  # noqa: S606 — a local path we just built
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def hostname() -> str:
    return platform.node() or "this-machine"
