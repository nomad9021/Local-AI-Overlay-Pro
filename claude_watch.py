"""
Claude watcher — is Claude running on this machine, how many sessions are open,
what each one is doing, and how many tokens have gone through them.

Three local sources, all of them files Claude Code already maintains; nothing is
asked of the network and no credentials are read:

1. `~/.claude/sessions/<pid>.json` — one file per live Claude Code session,
   carrying its pid, session id, working directory, a human-readable name, and a
   `status` of "busy" or "idle". That is the authoritative session registry.
   It is *not* self-cleaning: a session that crashed leaves its file behind, so
   every entry is checked against the live process table before being believed.
   Each file also records `procStart`, the Windows FILETIME of the process
   start, which converts exactly to psutil's `create_time` (verified to the
   tenth of a second) — so a recycled pid can be told from the original.
2. The process table, for the desktop app. Electron runs one main process plus
   a dozen `--type=renderer`/`--type=utility` helpers, so counting bare
   processes would report "13 instances" of a single window. The main process is
   identified as a Claude process that owns `--type=` children and isn't in the
   session registry.
3. `~/.claude/projects/<slug>/<session>.jsonl` — the transcripts. Every
   assistant record carries a `message.usage` block (input, output, cache read,
   cache creation) plus a timestamp and model, which is where the token figures
   come from.

What this deliberately does *not* report is "percent of your plan used". That
number lives server-side and Claude Code only renders it in its own `/usage`
view; nothing on disk caches it. Reporting a made-up percentage would be worse
than reporting none, so the card shows measured token volume instead and the
README says plainly where the real percentage lives.

Scanning cost: transcripts are append-only, so each file is read once and then
only from its previous end offset. Lines are pre-filtered on a substring test
before being handed to the JSON parser, which keeps a day of history (38 files,
41 MB here) at about 0.1s, and the hourly buckets are cached on disk so a
restart doesn't re-read the backlog.
"""
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from error_log import log_exception
from platform_support import config_dir

# Process names Claude ships under, per platform.
CLAUDE_PROC_NAMES = {"claude.exe", "claude", "Claude.exe", "Claude"}

# Helper processes an Electron app spawns; never their own "instance".
HELPER_FLAG = "--type="
# The Chrome extension's native messaging host: a helper, not a session.
NATIVE_HOST_FLAG = "--chrome-native-host"

SESSION_POLL_S = 2.0       # session registry: cheap, a handful of small files
PROCESS_POLL_S = 5.0       # process table walk: coarser, it's the expensive one
USAGE_REFRESH_S = 15.0     # transcript rescan; incremental after the first pass

# Transcripts older than this are never opened. Bounds the first scan and keeps
# the on-disk bucket cache from growing without limit.
USAGE_HORIZON_S = 8 * 86400

CACHE_VERSION = 2
CACHE_NAME = "claude_usage_cache.json"

# Only lines containing all of these are handed to json.loads(). Transcripts are
# mostly user/attachment records; this skips parsing them entirely.
USAGE_HINTS = ('"usage"', '"assistant"')


def claude_home() -> Path:
    """Claude's config directory, honouring CLAUDE_CONFIG_DIR when it's set."""
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude"


@dataclass
class ClaudeSession:
    """One live Claude Code session."""
    pid: int
    session_id: str = ""
    name: str = ""
    cwd: str = ""
    status: str = "idle"       # "busy" | "idle" — straight from the registry
    entrypoint: str = ""       # "claude-desktop" | "sdk-cli" | ...
    version: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0

    @property
    def busy(self) -> bool:
        return self.status == "busy"

    @property
    def label(self) -> str:
        """Best short name: the session's own, else the folder it works in."""
        if self.name:
            return self.name
        if self.cwd:
            return os.path.basename(self.cwd.rstrip("\\/")) or self.cwd
        return f"pid {self.pid}"

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.started_at) if self.started_at else 0.0


@dataclass
class ClaudeUsage:
    """Token totals over one window."""
    label: str = ""
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    messages: int = 0
    by_model: dict = field(default_factory=dict)

    @property
    def fresh_input(self) -> int:
        """Input that wasn't served from cache — the expensive kind."""
        return self.input_tokens + self.cache_write

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_write

    @property
    def empty(self) -> bool:
        return self.messages == 0


@dataclass
class ClaudeStatus:
    """Everything the card draws."""
    available: bool = False        # ~/.claude exists at all
    app_running: bool = False
    app_helpers: int = 0
    sessions: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)   # label -> ClaudeUsage
    scanned_files: int = 0
    scan_seconds: float = 0.0
    usage_error: str = ""

    @property
    def busy_sessions(self) -> int:
        return sum(1 for s in self.sessions if s.busy)

    @property
    def idle_sessions(self) -> int:
        return sum(1 for s in self.sessions if not s.busy)

    @property
    def running(self) -> bool:
        return self.app_running or bool(self.sessions)


def _psutil():
    try:
        import psutil
        return psutil
    except Exception:
        return None


def _filetime_to_unix(raw) -> float:
    """Windows FILETIME (100ns ticks since 1601) -> unix seconds."""
    try:
        return int(raw) / 1e7 - 11644473600
    except (TypeError, ValueError):
        return 0.0


def read_sessions(home: Path = None) -> list:
    """Live Claude Code sessions, stale registry entries dropped.

    A session file is believed only when its pid is live *and* that process
    started when the file says it did — otherwise a recycled pid would
    resurrect a session that ended days ago.
    """
    home = home or claude_home()
    folder = home / "sessions"
    if not folder.is_dir():
        return []

    psutil = _psutil()
    sessions = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue  # being rewritten, or truncated by a crash
        pid = data.get("pid")
        if not isinstance(pid, int):
            continue
        if not _process_matches(psutil, pid, data.get("procStart")):
            continue
        sessions.append(ClaudeSession(
            pid=pid,
            session_id=str(data.get("sessionId", "")),
            name=str(data.get("name", "") or ""),
            cwd=str(data.get("cwd", "") or ""),
            status=str(data.get("status", "idle") or "idle"),
            entrypoint=str(data.get("entrypoint", "") or ""),
            version=str(data.get("version", "") or ""),
            started_at=(data.get("startedAt") or 0) / 1000.0,
            updated_at=(data.get("updatedAt") or 0) / 1000.0,
        ))

    # Busy first, then most recently active: the card shows the top few.
    sessions.sort(key=lambda s: (not s.busy, -(s.updated_at or s.started_at)))
    return sessions


def _process_matches(psutil, pid: int, proc_start) -> bool:
    if psutil is None:
        return True  # can't verify; trusting the registry beats showing nothing
    try:
        proc = psutil.Process(pid)
        if (proc.name() or "") not in CLAUDE_PROC_NAMES:
            return False
        expected = _filetime_to_unix(proc_start)
        if expected <= 0:
            return True  # no start stamp recorded; pid existence is all we have
        return abs(proc.create_time() - expected) < 2.0
    except Exception:
        return False   # gone, or not ours to inspect


def detect_app(session_pids=None) -> tuple:
    """(app_running, helper_count) for the Claude desktop app.

    Electron's helpers all carry `--type=`; the main process is the one that
    owns them. Claude Code sessions are excluded by pid so a terminal session
    is never mistaken for the desktop app.
    """
    psutil = _psutil()
    if psutil is None:
        return False, 0

    session_pids = set(session_pids or ())
    mains, helpers, parents = {}, 0, set()
    try:
        for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
            try:
                if (proc.info.get("name") or "") not in CLAUDE_PROC_NAMES:
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if HELPER_FLAG in cmdline:
                    helpers += 1
                    parents.add(proc.info.get("ppid"))
                    continue
                if NATIVE_HOST_FLAG in cmdline:
                    continue
                if proc.info["pid"] in session_pids:
                    continue
                mains[proc.info["pid"]] = cmdline
            except Exception:
                continue
    except Exception:
        log_exception("detect_app process_iter")
        return False, 0

    # A main process is one with helper children. If helpers were seen but their
    # parent wasn't classified (access denied on the parent, say), still report
    # the app as running rather than claiming it isn't.
    owns_helpers = [pid for pid in mains if pid in parents]
    if owns_helpers:
        return True, helpers
    return (helpers > 0), helpers


class UsageIndex:
    """Hourly token buckets built incrementally from the transcripts.

    Buckets are keyed by UTC hour so any window — the rolling 5 hours that
    matches Claude's own session window, the local day, the week — is a sum over
    a slice, without rescanning anything. File offsets and buckets persist to
    the app's config dir so a restart resumes instead of re-reading the backlog.
    """

    def __init__(self, home: Path = None, cache_path: Path = None):
        self._home = home or claude_home()
        self._cache_path = cache_path or (config_dir() / CACHE_NAME)
        self._files: dict = {}     # path -> {"offset": int, "mtime": float}
        self._buckets: dict = {}   # hour_epoch -> {model: [in, out, cr, cw, msgs]}
        self._loaded = False
        self.last_scan_files = 0
        self.last_scan_seconds = 0.0
        self.error = ""

    # ---- persistence ----------------------------------------------------- #

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if raw.get("version") != CACHE_VERSION:
                return
            self._files = {k: v for k, v in (raw.get("files") or {}).items()}
            self._buckets = {
                int(hour): {m: list(v) for m, v in models.items()}
                for hour, models in (raw.get("buckets") or {}).items()
            }
        except FileNotFoundError:
            pass
        except Exception:
            log_exception("UsageIndex.load")   # corrupt cache: rebuild from scratch

    def save(self) -> None:
        try:
            self._prune()
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "version": CACHE_VERSION,
                "files": self._files,
                "buckets": {str(h): v for h, v in self._buckets.items()},
            }), encoding="utf-8")
            os.replace(tmp, self._cache_path)
        except Exception:
            log_exception("UsageIndex.save")

    def _prune(self) -> None:
        cutoff = time.time() - USAGE_HORIZON_S
        for hour in [h for h in self._buckets if h < cutoff]:
            del self._buckets[hour]
        for path in [p for p, meta in self._files.items()
                     if meta.get("mtime", 0) < cutoff and not os.path.exists(p)]:
            del self._files[path]

    # ---- scanning --------------------------------------------------------- #

    def refresh(self) -> None:
        self.load()
        started = time.perf_counter()
        scanned = 0
        horizon = time.time() - USAGE_HORIZON_S
        projects = self._home / "projects"
        if not projects.is_dir():
            self.error = "no transcripts found"
            return
        self.error = ""

        try:
            paths = list(projects.glob("*/*.jsonl"))
        except Exception:
            log_exception("UsageIndex.refresh glob")
            return

        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < horizon:
                continue
            key = str(path)
            known = self._files.get(key)
            offset = int(known.get("offset", 0)) if known else 0
            if known and stat.st_size == offset:
                continue                     # nothing appended since last pass
            if stat.st_size < offset:
                offset = 0                   # rewritten/truncated: start over
            if self._scan_file(path, offset, stat):
                scanned += 1

        self.last_scan_files = scanned
        self.last_scan_seconds = time.perf_counter() - started
        if scanned:
            self.save()

    def _scan_file(self, path: Path, offset: int, stat) -> bool:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                chunk = handle.read()
                new_offset = handle.tell()
        except OSError:
            return False

        for line in chunk.splitlines():
            if not all(hint in line for hint in USAGE_HINTS):
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue   # a line still being written; next pass re-reads it
            if record.get("type") != "assistant":
                continue
            message = record.get("message") or {}
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            hour = _hour_of(record.get("timestamp"))
            if hour is None:
                continue
            model = str(message.get("model") or "unknown")
            slot = self._buckets.setdefault(hour, {}).setdefault(model, [0, 0, 0, 0, 0])
            slot[0] += int(usage.get("input_tokens") or 0)
            slot[1] += int(usage.get("output_tokens") or 0)
            slot[2] += int(usage.get("cache_read_input_tokens") or 0)
            slot[3] += int(usage.get("cache_creation_input_tokens") or 0)
            slot[4] += 1

        self._files[str(path)] = {"offset": new_offset, "mtime": stat.st_mtime}
        return True

    # ---- queries ---------------------------------------------------------- #

    def window(self, seconds: float, label: str) -> ClaudeUsage:
        return self.since(time.time() - seconds, label, seconds)

    def since(self, epoch: float, label: str, seconds: float = 0.0) -> ClaudeUsage:
        # Buckets are whole hours, so a window edge mid-hour includes that hour.
        first = int(epoch // 3600) * 3600
        out = ClaudeUsage(label=label, seconds=seconds)
        for hour, models in self._buckets.items():
            if hour < first:
                continue
            for model, counts in models.items():
                out.input_tokens += counts[0]
                out.output_tokens += counts[1]
                out.cache_read += counts[2]
                out.cache_write += counts[3]
                out.messages += counts[4]
                agg = out.by_model.setdefault(model, [0, 0, 0])
                agg[0] += counts[0] + counts[3]
                agg[1] += counts[1]
                agg[2] += counts[4]
        return out

    def today(self, label: str = "today") -> ClaudeUsage:
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        return self.since(midnight, label)


def _hour_of(timestamp) -> int:
    """ISO-8601 UTC stamp -> epoch seconds truncated to the hour."""
    if not isinstance(timestamp, str) or len(timestamp) < 13:
        return None
    try:
        # "2026-08-19T21:08:22.162Z" -> parse only to the hour; cheaper than a
        # full datetime parse and these are always UTC with a Z suffix.
        year, month, day = int(timestamp[0:4]), int(timestamp[5:7]), int(timestamp[8:10])
        hour = int(timestamp[11:13])
        return int(_timegm(year, month, day, hour))
    except (ValueError, TypeError):
        return None


def _timegm(year, month, day, hour) -> int:
    import calendar
    return calendar.timegm((year, month, day, hour, 0, 0, 0, 0, 0))


class ClaudeWatcher(QObject):
    """Polls the session registry, the process table and the transcripts.

    All of it runs on one worker thread: the registry every couple of seconds
    (small file reads), the process table less often (a full walk is the
    expensive part), and the transcripts on a slower cadence still, since after
    the first pass each file is only read from where the last pass stopped.
    """

    updated = Signal(object)   # ClaudeStatus

    def __init__(self, parent=None, usage_refresh_s: float = USAGE_REFRESH_S):
        super().__init__(parent)
        self._index = UsageIndex()
        self._usage_refresh_s = usage_refresh_s
        self._stop = threading.Event()
        self._thread = None
        self._status = ClaudeStatus()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="claude-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def status(self) -> ClaudeStatus:
        with self._lock:
            return self._status

    def _run(self) -> None:
        last_proc = 0.0
        last_usage = 0.0
        app_running, helpers = False, 0
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                home = claude_home()
                available = home.is_dir()
                sessions = read_sessions(home) if available else []

                if now - last_proc >= PROCESS_POLL_S:
                    app_running, helpers = detect_app({s.pid for s in sessions})
                    last_proc = now

                if available and (now - last_usage >= self._usage_refresh_s):
                    self._index.refresh()
                    last_usage = now

                usage = {}
                if available:
                    usage["5h"] = self._index.window(5 * 3600, "5h")
                    usage["today"] = self._index.today()
                    usage["7d"] = self._index.window(7 * 86400, "7d")

                status = ClaudeStatus(
                    available=available,
                    app_running=app_running,
                    app_helpers=helpers,
                    sessions=sessions,
                    usage=usage,
                    scanned_files=self._index.last_scan_files,
                    scan_seconds=self._index.last_scan_seconds,
                    usage_error=self._index.error,
                )
                with self._lock:
                    self._status = status
                self.updated.emit(status)
            except Exception:
                # This loop must outlive any single bad read: a half-written
                # registry file or a transcript being rotated underneath us is
                # normal, and a dead watcher thread would silently freeze the
                # card at whatever it last showed.
                log_exception("ClaudeWatcher._run")
            self._stop.wait(SESSION_POLL_S)


def fmt_tokens(value: int) -> str:
    """Compact token counts: 1.2B / 34.5M / 912K / 403."""
    value = int(value or 0)
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= limit:
            scaled = value / limit
            return f"{scaled:.1f}{suffix}" if scaled < 100 else f"{scaled:.0f}{suffix}"
    return str(value)
