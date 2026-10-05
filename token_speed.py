"""
TokenSpeedTracker — real per-request tok/s read directly from Ollama's own
server log, instead of a reverse proxy. Ollama's llama.cpp backend logs a
line like:

    slot print_timing: id 0 | task 0 | eval time = 973.95 ms / 68 tokens
    (14.32 ms per token, 69.82 tokens per second)

for every completed generation, regardless of which client made the
request (Ollama CLI, Open WebUI, custom scripts) — so this works with zero
client reconfiguration, unlike a reverse proxy.

Tradeoffs, called out explicitly:
- This depends on Ollama's internal log text format, not a public/stable
  API. A future Ollama release could change the wording and break parsing.
- The log line itself carries no model name. We attribute tok/s to
  whichever model most recently appeared in a "template selection" log
  line (emitted once per model load). This is correct for the common case
  of talking to one model at a time; if two+ models are genuinely warm and
  receiving interleaved requests concurrently, tok/s can be briefly
  misattributed to the wrong model.
- Linux support assumes Ollama runs as the standard `ollama` systemd
  service (the default for the official install script) so `journalctl -u
  ollama` has something to read. If Ollama is run manually in a terminal
  instead, there's no log source to tail and tok/s stays 0.0 — same
  graceful-degrade behavior as having no GPU backend available.
"""
import platform
import re
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from error_log import log_exception

MODEL_LINE_RE = re.compile(r'msg="template selection" model=(\S+)')
EVAL_LINE_RE = re.compile(
    r"eval time\s*=\s*[\d.]+\s*ms\s*/\s*(\d+)\s*tokens\s*\(\s*[\d.]+\s*ms per token,\s*([\d.]+)\s*tokens per second\)"
)
LIBRARY_PREFIX = "registry.ollama.ai/library/"


def _short_model_name(raw: str) -> str:
    return raw[len(LIBRARY_PREFIX):] if raw.startswith(LIBRARY_PREFIX) else raw


class LogSource(ABC):
    """Cross-platform source of Ollama server-log lines, delivered as they arrive."""

    @abstractmethod
    def start(self, on_line: Callable[[str], None]) -> bool:
        """Begin delivering new log lines to on_line(). Returns False if unavailable."""

    @abstractmethod
    def stop(self) -> None:
        ...


class WindowsLogFileSource(LogSource):
    """Polls Ollama's rotating server.log, tailing only newly-appended lines."""

    def __init__(self, path: Optional[Path] = None, poll_ms: int = 750):
        import os
        self._path = path or Path(os.environ.get("LOCALAPPDATA", "")) / "Ollama" / "server.log"
        self._poll_ms = poll_ms
        self._timer: Optional[QTimer] = None
        self._pos = 0
        self._on_line: Optional[Callable[[str], None]] = None

    def start(self, on_line: Callable[[str], None]) -> bool:
        if not self._path.exists():
            return False
        self._on_line = on_line
        self._pos = self._path.stat().st_size  # tail from EOF, ignore historical lines
        self._timer = QTimer()
        self._timer.timeout.connect(self._poll)
        self._timer.start(self._poll_ms)
        return True

    def _poll(self):
        try:
            size = self._path.stat().st_size
            if size < self._pos:
                self._pos = 0  # rotated: Ollama renamed this to server-1.log and started fresh
            if size == self._pos:
                return
            with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._pos)
                new_data = f.read()
                self._pos = f.tell()
        except Exception:
            return

        # Each line is handled defensively: this method runs on a QTimer tick, and an
        # uncaught exception here (e.g. from an unanticipated log line, or a line torn
        # mid-write by the concurrent Ollama process) must never propagate out of a Qt
        # slot. Under pythonw.exe (no console, as used by the Startup-folder autostart
        # shortcut) sys.stderr can be None, so Python's default unhandled-exception
        # printing can itself raise — silently breaking this timer's future ticks
        # without crashing the app, which reads exactly like "tok/s stuck at 0 forever."
        for line in new_data.splitlines():
            try:
                self._on_line(line)
            except Exception:
                log_exception("WindowsLogFileSource._poll on_line")
                continue

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None


class LinuxJournaldSource(LogSource):
    """Tails `journalctl -u ollama -f` — async via QProcess, no polling needed."""

    def __init__(self, unit: str = "ollama"):
        self._unit = unit
        self._process: Optional[QProcess] = None
        self._on_line: Optional[Callable[[str], None]] = None

    def start(self, on_line: Callable[[str], None]) -> bool:
        if shutil.which("journalctl") is None:
            return False
        self._on_line = on_line
        self._process = QProcess()
        self._process.setProgram("journalctl")
        self._process.setArguments(["-u", self._unit, "-f", "-n", "0", "--output=cat"])
        self._process.readyReadStandardOutput.connect(self._read)
        self._process.start()
        return self._process.waitForStarted(1000)

    def _read(self):
        if self._process is None or self._on_line is None:
            return
        data = bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace")
        for line in data.splitlines():
            try:
                self._on_line(line)
            except Exception:
                log_exception("LinuxJournaldSource._read on_line")
                continue

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()
            self._process = None


def get_log_source() -> Optional[LogSource]:
    system = platform.system()
    if system == "Windows":
        return WindowsLogFileSource()
    if system == "Linux":
        return LinuxJournaldSource()
    return None


class TokenSpeedTracker(QObject):
    speedUpdated = Signal(str, float)  # model_name, tok_s

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source = get_log_source()
        self._last_model: Optional[str] = None

    def start(self) -> bool:
        if self._source is None:
            return False
        return self._source.start(self._handle_line)

    def stop(self) -> None:
        if self._source is not None:
            self._source.stop()

    def set_active_models(self, names: list) -> None:
        """Seed/correct attribution from /api/ps, which OllamaMonitor already polls.

        A model that was already warm before this tracker started never emits a
        fresh "template selection" log line, so relying on the log alone would
        leave _last_model stuck at None indefinitely. When exactly one model is
        active there's no ambiguity, so /api/ps is trusted outright. With 2+
        concurrent models we fall back to the log-based sticky heuristic instead,
        since /api/ps alone can't say which one is actively generating.
        """
        if len(names) == 1:
            self._last_model = names[0]
        elif len(names) == 0:
            self._last_model = None

    def _handle_line(self, line: str):
        model_match = MODEL_LINE_RE.search(line)
        if model_match:
            self._last_model = _short_model_name(model_match.group(1))
            return

        if "prompt eval time" in line:
            return

        eval_match = EVAL_LINE_RE.search(line)
        if eval_match and self._last_model:
            tok_s = float(eval_match.group(2))
            self.speedUpdated.emit(self._last_model, tok_s)
