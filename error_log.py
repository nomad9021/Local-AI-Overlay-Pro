"""
Tiny shared exception logger.

Several places in this app deliberately catch-and-continue on any exception,
because an uncaught exception in a Qt timer slot can silently kill that timer
forever when running under pythonw.exe (no console, sys.stderr=None — how the
Startup-folder autostart shortcut runs). That's the right behavior for staying
alive, but swallowing silently makes a real bug indistinguishable from normal
operation. This logs what was caught to a file instead, so a recurrence can
actually be diagnosed rather than guessed at.
"""
import traceback
from pathlib import Path

_LOG_PATH = Path(__file__).parent / "error.log"


def log_exception(context: str) -> None:
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"--- {context} ---\n")
            traceback.print_exc(file=f)
    except Exception:
        pass  # logging must never itself become a new source of failure
