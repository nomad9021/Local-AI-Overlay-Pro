"""
Stopping Ollama from the HUD.

Two levels, both driven from the overlay's header:

- `unload_models()` — asks Ollama to drop the resident models (keep_alive: 0)
  but leaves the server running. Frees VRAM without ending the session.
- `stop_ollama()` — the full stop the STOP button performs: unload first (so
  the runners get a chance to exit cleanly and release VRAM), then terminate
  the model runners, then `ollama serve` itself, then the desktop/tray app if
  present. The desktop app is included deliberately: it supervises the server
  and would otherwise restart what we just killed.

Termination is SIGTERM-equivalent first with a short grace period, escalating
to a hard kill only for processes that don't exit. Runs off the UI thread via
`OllamaController`, since HTTP calls plus process waits take a second or two and
freezing a frameless always-on-top HUD reads as a crash.
"""
import json
import os
import shutil
import subprocess
import threading
import urllib.request

from PySide6.QtCore import QObject, Signal

from error_log import log_exception
from model_state import is_runner_process

# Names of the Ollama processes themselves (not the model runners).
SERVER_NAMES = {"ollama.exe", "ollama"}
APP_NAMES = {"ollama app.exe", "ollama app", "ollama-app.exe"}

TERM_GRACE_S = 4.0


def _post(base_url: str, path: str, payload: dict, timeout: float):
    req = urllib.request.Request(
        base_url + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def resident_model_names(base_url: str = "http://localhost:11434", timeout: float = 2.0) -> list:
    try:
        with urllib.request.urlopen(f"{base_url}/api/ps", timeout=timeout) as resp:
            return [m.get("name") or m.get("model", "")
                    for m in json.load(resp).get("models", [])]
    except Exception:
        return []


def unload_models(base_url: str = "http://localhost:11434") -> int:
    """Ask Ollama to unload every resident model. Returns how many it accepted."""
    unloaded = 0
    for name in resident_model_names(base_url):
        if not name:
            continue
        try:
            # keep_alive: 0 with an empty prompt is Ollama's documented way to
            # evict a model immediately without generating anything.
            _post(base_url, "/api/generate",
                  {"model": name, "keep_alive": 0, "prompt": ""}, timeout=8.0)
            unloaded += 1
        except Exception:
            log_exception(f"unload_models {name}")
    return unloaded


def _terminate(procs) -> int:
    """Terminate then kill; returns how many processes are gone afterwards."""
    import psutil

    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=TERM_GRACE_S)
    for proc in alive:
        try:
            proc.kill()
        except Exception:
            pass
    if alive:
        gone2, _ = psutil.wait_procs(alive, timeout=2.0)
        gone = list(gone) + list(gone2)
    return len(gone)


def _collect_runners(psutil) -> list:
    out = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if is_runner_process(proc.info.get("name"), proc.info.get("cmdline") or []):
                out.append(proc)
        except Exception:
            continue
    return out


def _collect(psutil, wanted_names, require_serve_arg=False) -> list:
    out = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in wanted_names:
                continue
            if require_serve_arg:
                cmdline = [a.lower() for a in (proc.info.get("cmdline") or [])]
                # Don't take down unrelated `ollama run`/`ollama pull` CLI
                # invocations — only the process actually serving the API.
                if "serve" not in cmdline:
                    continue
            out.append(proc)
        except Exception:
            continue
    return out


def _taskkill(image_names) -> int:
    """Fallback used only when psutil isn't installed (Windows only)."""
    killed = 0
    for image in image_names:
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/IM", image],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode == 0:
                killed += 1
        except Exception:
            log_exception(f"_taskkill {image}")
    return killed


def _stop_systemd_service() -> "tuple | None":
    """Linux: stop `ollama.service` through systemd, if that's what runs it.

    The official Linux installer runs Ollama as a system service with
    Restart=always, so killing the process just has systemd start a fresh one a
    few seconds later — the STOP button would look broken. Stopping the unit is
    the only stop that sticks. That needs root: try non-interactively first
    (works when polkit grants it, or when this runs as root), then via pkexec,
    which shows the desktop's own password prompt.

    Returns (ok, message), or None if no active ollama service was found.
    """
    if os.name == "nt" or shutil.which("systemctl") is None:
        return None
    try:
        active = subprocess.run(["systemctl", "is-active", "--quiet", "ollama"],
                                timeout=5).returncode == 0
    except Exception:
        return None
    if not active:
        return None

    attempts = [["systemctl", "--no-ask-password", "stop", "ollama"]]
    if shutil.which("pkexec") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        attempts.append(["pkexec", "systemctl", "stop", "ollama"])
    for cmd in attempts:
        try:
            if subprocess.run(cmd, capture_output=True, text=True, timeout=120).returncode == 0:
                return True, "Stopped ollama.service"
        except Exception:
            log_exception(f"_stop_systemd_service {cmd[0]}")
    return False, "ollama.service needs root to stop — run: sudo systemctl stop ollama"


def stop_ollama(base_url: str = "http://localhost:11434") -> tuple:
    """Unload models, then stop the runners, the server, and the desktop app.

    `base_url` scopes only the unload step. Process termination matches on
    process *name*, so this stops every local Ollama server regardless of which
    port it serves — fine for the single-localhost-instance case this overlay is
    built for, but worth knowing if you ever run two instances on one machine.

    Returns (ok, message) for display in a toast.
    """
    unloaded = unload_models(base_url)

    service_result = _stop_systemd_service()
    if service_result is not None:
        return service_result

    try:
        import psutil
    except Exception:
        if os.name != "nt":
            return False, "psutil not installed — cannot stop the server"
        killed = _taskkill(["llama-server.exe", "ollama app.exe", "ollama.exe"])
        if killed:
            return True, f"Killed Ollama ({unloaded} model(s) unloaded first)"
        return False, "Nothing to stop — Ollama wasn't running"

    runners = _collect_runners(psutil)
    servers = _collect(psutil, SERVER_NAMES, require_serve_arg=True)
    apps = _collect(psutil, APP_NAMES)

    if not (runners or servers or apps):
        return False, "Nothing to stop — Ollama wasn't running"

    # Runners first so the server doesn't try to restart or re-reap them, then
    # the app last: killing the supervisor before its child can leave the child
    # orphaned but alive.
    stopped = _terminate(runners) + _terminate(servers) + _terminate(apps)

    parts = []
    if servers:
        parts.append("server")
    if runners:
        parts.append(f"{len(runners)} runner(s)")
    if apps:
        parts.append("desktop app")
    detail = ", ".join(parts) if parts else f"{stopped} process(es)"
    return True, f"Stopped {detail}"


class OllamaController(QObject):
    """Runs the stop/unload calls on worker threads, reporting back on the UI thread.

    Both operations make HTTP calls and wait on process exits — seconds of work.
    On the UI thread that would freeze a frameless always-on-top HUD, which
    looks indistinguishable from a hang, so both are threaded and answer via
    signals. Qt queues a signal emitted from a Python thread onto the UI thread,
    so the connected slots stay on the main thread as usual.
    """

    stopFinished = Signal(bool, str)   # ok, message
    unloadFinished = Signal(int)       # models unloaded

    def __init__(self, base_url: str = "http://localhost:11434", parent=None):
        super().__init__(parent)
        self._base_url = base_url
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def stop_async(self) -> None:
        self._run_async(self._stop)

    def unload_async(self) -> None:
        self._run_async(self._unload)

    def _run_async(self, target) -> None:
        if self._busy:
            return  # one shutdown-ish operation at a time
        self._busy = True
        threading.Thread(target=target, daemon=True).start()

    def _stop(self) -> None:
        try:
            ok, message = stop_ollama(self._base_url)
        except Exception:
            log_exception("OllamaController._stop")
            ok, message = False, "Stop failed — see error.log"
        finally:
            self._busy = False
        self.stopFinished.emit(ok, message)

    def _unload(self) -> None:
        try:
            count = unload_models(self._base_url)
        except Exception:
            log_exception("OllamaController._unload")
            count = 0
        finally:
            self._busy = False
        self.unloadFinished.emit(count)
