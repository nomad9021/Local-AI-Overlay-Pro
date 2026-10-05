"""
Per-model run state: is this model generating right now, or just sitting warm
waiting to be used?

Ollama's `/api/ps` deliberately doesn't answer that — it lists what's resident
in VRAM and when each entry expires, and nothing more. Verified against Ollama
0.30.10: during a generation `expires_at` stays frozen at its old value and the
countdown keeps ticking down; the instant the request completes it snaps forward
to now + keep_alive. So /api/ps alone can detect *completions* but can't see a
request that's currently in flight.

What it can be told from, in descending order of fidelity — each layer falls
back to the next so the HUD degrades to something truthful instead of breaking:

1. `/slots` on the model's own llama-server runner, sampled continuously by
   `SlotsSampler` on a background thread. Every resident model runs as its own
   `llama-server.exe` subprocess, and its command line carries both
   `--port <n>` and `--model <blob path>`. Matching that blob against the
   `FROM <blob path>` line from `/api/show` gives an exact runner->model
   mapping, and the runner's `/slots` reports `is_processing` per slot — an
   authoritative busy flag — plus `next_token.n_decoded`, whose growth over
   time is real generation speed for *any* client's request.
2. Per-process GPU SM utilization via NVML, keyed on the runner PID. Verified
   to read 23-69% while a runner generates and drop to zero within ~2s of
   finishing. Used when `/slots` is unavailable (a llama.cpp build started with
   `--no-slots`, or a future runner that drops the endpoint).
3. Completion-only detection from the `expires_at` jump described above, which
   needs nothing but /api/ps and therefore always works. Handled by the caller
   (monitor.py), since it owns the poll-to-poll history.

Why the sampler runs continuously rather than on the UI poll tick: speed is the
*growth* of a token counter, so it needs two samples spanning the generation.
A fast model answering in a quarter second finishes between any two UI-rate
polls, leaving nothing to difference — which is exactly why small models showed
no tok/s at all. Measured cost of sampling at 100ms on this hardware: a
`/slots` response is ~1.2 KB and takes 0.30ms over a kept-alive connection, and
an A/B test of generation throughput with the sampler running versus stopped
came out at 44.1 vs 44.3 tok/s — a 0.5% difference, inside run-to-run noise.

Why not the server log: the previous tok/s implementation (token_speed.py)
parsed `slot print_timing` lines out of Ollama's server.log. Ollama 0.30.10
emits no such line — and when `ollama serve` is started by hand rather than by
the desktop app, it doesn't write `%LOCALAPPDATA%\\Ollama\\server.log` at all.
That's why speed is read from the runner here instead.
"""
import http.client
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from PySide6.QtCore import QObject, Signal

from error_log import log_exception

# Runner executable names across Ollama versions/platforms.
RUNNER_NAMES = {
    "llama-server.exe", "llama-server",
    "ollama_llama_server.exe", "ollama_llama_server",
}

# Newer Ollama builds (the default on current Linux installs) don't ship a
# separate runner binary: each model runs as `ollama runner --model <blob>
# --port <n>`, i.e. the main executable with a `runner` subcommand.
OLLAMA_EXE_NAMES = {"ollama", "ollama.exe"}


def is_runner_process(name: str, cmdline: list) -> bool:
    name = (name or "").lower()
    if name in RUNNER_NAMES:
        return True
    if name in OLLAMA_EXE_NAMES:
        return any(str(arg).lower() == "runner" for arg in (cmdline or [])[1:3])
    return False

FROM_LINE_RE = re.compile(r"^FROM\s+(.+)$", re.M)

# Sampler cadence. 100ms is fast enough to catch a sub-second reply from a
# small model and costs ~3ms of CPU per second per runner (see module docstring).
SAMPLE_INTERVAL_S = 0.1
SAMPLE_IDLE_INTERVAL_S = 0.5   # nothing resident: idle instead of spinning
SAMPLE_TIMEOUT_S = 0.5

# Speed is measured across a window rather than between adjacent samples: at
# 100ms a single interval holds only ~4 tokens at 45 tok/s, so ±1 token of
# quantisation would swing the reading by a quarter. A 0.6s window smooths that
# while still producing a first number ~200ms into a generation.
SPEED_WINDOW_S = 0.6
# Shorter spans are refused: at 100ms sampling, one sample of quantisation error
# over a very short span produces a wild number. 80ms is low enough to catch a
# fast small model's sub-second reply, which is the whole point of sampling at
# this rate, while still spanning at least one full sampling interval.
SPEED_MIN_SPAN_S = 0.08

# Consecutive token-growth observations further apart than this are treated as a
# discontinuity rather than one long generation interval. A continuously
# generating model grows its counter every sampling interval; a longer gap means
# it spent part of that time not generating — queued behind another request, or
# loading — and averaging across the gap invents a slow rate. This is what made
# a 10-token reply report 4 tok/s: its two growth points sat 2.5s apart across a
# model load.
GROWTH_GAP_MAX_S = 0.35

# How long a runner that failed to answer /slots is skipped before retrying, so
# one wedged runner can't slow the whole sampling loop.
PORT_BACKOFF_S = 5.0

# Process/blob discovery is comparatively expensive (process table walk + an
# /api/show per model), so it only re-runs when the resident set changes or
# this long has passed — whichever comes first.
RESCAN_INTERVAL_S = 20.0

# NVML per-process SM utilization at or above this counts as "generating".
# Idle resident runners report no sample at all, so this only has to clear
# measurement noise.
SM_BUSY_PCT = 5


class ModelState(str, Enum):
    ACTIVE = "ACTIVE"    # generating right now
    WAITING = "WAITING"  # resident in VRAM, idle, counting down to keep_alive expiry
    STOPPED = "STOPPED"  # not resident: unloaded, expired, or never loaded this session


@dataclass
class Runner:
    pid: int
    port: Optional[int]
    blob: str
    ctx: Optional[int]


@dataclass
class Activity:
    """One model's live activity, plus where the answer came from."""
    busy: Optional[bool] = None   # None = couldn't be determined at this layer
    tok_s: Optional[float] = None
    source: str = "none"          # "slots" | "nvml" | "none"


def _norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.normpath(path.strip().strip('"')))
    except Exception:
        return path


def _iter_runner_processes():
    """Yield (pid, cmdline_list) for each live Ollama model runner.

    psutil is preferred; on Windows without it, fall back to one CIM query.
    Yields nothing (rather than raising) if neither path is available, which
    downgrades state detection to the /api/ps-only layer in monitor.py.
    """
    try:
        import psutil
    except Exception:
        psutil = None

    if psutil is not None:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if is_runner_process(proc.info.get("name"), cmdline):
                    yield proc.info["pid"], cmdline
            except Exception:
                continue  # process died mid-walk, or access denied
        return

    if os.name != "nt":
        return

    try:
        # -NoProfile keeps this from executing the user's PowerShell profile.
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"name='llama-server.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return
        data = json.loads(completed.stdout)
        if isinstance(data, dict):
            data = [data]
        for entry in data:
            cmdline = entry.get("CommandLine") or ""
            # Good enough for the flags we read (--model/--port/-c), all of
            # which are unquoted single tokens in Ollama's own invocation.
            yield int(entry["ProcessId"]), cmdline.split()
    except Exception:
        log_exception("_iter_runner_processes powershell fallback")


def _parse_runner(pid: int, cmdline: list) -> Optional[Runner]:
    blob = port = ctx = None
    for i, arg in enumerate(cmdline):
        nxt = cmdline[i + 1] if i + 1 < len(cmdline) else None
        if nxt is None:
            continue
        if arg == "--model":
            blob = nxt
        elif arg == "--port":
            try:
                port = int(nxt)
            except ValueError:
                pass
        elif arg == "-c":
            try:
                ctx = int(nxt)
            except ValueError:
                pass
    if not blob:
        return None
    return Runner(pid=pid, port=port, blob=_norm(blob), ctx=ctx)


class RunnerIndex:
    """Maps resident model names to their llama-server runner process.

    Blob path alone isn't always unique: several Ollama tags can share one GGUF
    blob and differ only in Modelfile parameters (e.g. qwen3-8b-40k vs
    qwen3-8b-64k vs qwen3:8b are the same weights at different context sizes).
    Those are disambiguated by context length, which /api/ps reports as
    `context_length` and the runner takes as `-c`. Anything still ambiguous is
    left unmapped on purpose — an unmapped model just falls back to a coarser
    state signal, whereas a wrong mapping would attribute one model's
    generation to another.
    """

    def __init__(self, base_url: str = "http://localhost:11434"):
        self._base_url = base_url
        self._runners: list[Runner] = []
        self._blob_by_name: dict[str, str] = {}
        self._last_scan = 0.0
        self._last_resident: tuple = ()

    def refresh(self, resident: list, now: Optional[float] = None) -> None:
        """resident: list of objects with .name and .context_length."""
        now = now if now is not None else time.monotonic()
        key = tuple(sorted(m.name for m in resident))
        if key != self._last_resident or (now - self._last_scan) > RESCAN_INTERVAL_S:
            self._scan_processes()
            self._last_scan = now
            self._last_resident = key

        for model in resident:
            if model.name not in self._blob_by_name:
                blob = self._fetch_blob(model.name)
                if blob:
                    self._blob_by_name[model.name] = blob

        # A blob we can't account for means a model was re-pulled (new blob) or
        # loaded under a name we haven't resolved — drop the cache so the next
        # refresh re-resolves every name rather than leaving rows unmapped.
        known = {self._blob_by_name.get(m.name) for m in resident}
        if self._runners and not any(r.blob in known for r in self._runners):
            self._blob_by_name.clear()

    def _scan_processes(self) -> None:
        found = []
        for pid, cmdline in _iter_runner_processes():
            runner = _parse_runner(pid, cmdline)
            if runner is not None:
                found.append(runner)
        self._runners = found

    def _fetch_blob(self, name: str) -> Optional[str]:
        try:
            body = json.dumps({"name": name}).encode()
            req = urllib.request.Request(
                f"{self._base_url}/api/show", data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                modelfile = json.load(resp).get("modelfile", "")
        except Exception:
            return None
        match = FROM_LINE_RE.search(modelfile or "")
        return _norm(match.group(1)) if match else None

    def runner_for(self, name: str, ctx: Optional[int] = None) -> Optional[Runner]:
        if len(self._runners) == 1 and len(self._last_resident) == 1:
            return self._runners[0]  # unambiguous regardless of blob resolution

        blob = self._blob_by_name.get(name)
        if not blob:
            return None
        candidates = [r for r in self._runners if r.blob == blob]
        if len(candidates) == 1:
            return candidates[0]
        if ctx is not None:
            narrowed = [r for r in candidates if r.ctx == ctx]
            if len(narrowed) == 1:
                return narrowed[0]
        return None  # ambiguous: better no answer than a wrong one


def read_slot_progress(slots: list) -> tuple:
    """(n_decoded, id_task) from the first usable slot, else (None, None).

    `next_token` is a one-element list in Ollama 0.30.10's bundled runner but a
    bare object in other llama.cpp builds; both are accepted.
    """
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        nxt = slot.get("next_token")
        if isinstance(nxt, list):
            nxt = nxt[0] if nxt and isinstance(nxt[0], dict) else None
        if isinstance(nxt, dict) and isinstance(nxt.get("n_decoded"), int):
            return nxt["n_decoded"], slot.get("id_task")
    return None, None


@dataclass
class _PortState:
    """Sampling state for one runner port."""
    task: object = None                 # id_task the samples belong to
    samples: deque = None               # (monotonic time, n_decoded) at growth points
    speed: Optional[float] = None       # last published tok/s
    measured_task: object = None        # task a reading has already been published for

    def reset(self, task) -> None:
        self.task = task
        self.samples = deque()


class SlotsSampler(QObject):
    """Continuously samples every known runner's /slots on a worker thread.

    Owns all `/slots` I/O so the UI thread never blocks on it, and samples fast
    enough that a generation lasting only a couple of hundred milliseconds still
    spans several samples. Connections are kept alive per runner: reconnecting
    at this rate would churn ephemeral ports for no reason.

    The UI thread interacts with it in two ways only — `set_targets()` to say
    which model maps to which runner port, and `snapshot()` to read the latest
    per-model result — both guarded by one lock around plain dict swaps.
    """

    # Emitted when something starts generating, so the monitor can refresh the
    # HUD immediately instead of waiting out its next poll interval.
    activityStarted = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._targets: dict = {}    # model name -> runner port
        self._results: dict = {}    # model name -> Activity
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._any_busy = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="slots-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.5)

    def set_targets(self, targets: dict) -> None:
        with self._lock:
            self._targets = dict(targets)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._results)

    def _run(self) -> None:
        conns: dict = {}      # port -> HTTPConnection
        states: dict = {}     # port -> _PortState
        blocked: dict = {}    # port -> monotonic time to retry after

        while not self._stop.is_set():
            with self._lock:
                targets = dict(self._targets)

            if not targets:
                self._drop_unused(conns, set())
                self._stop.wait(SAMPLE_IDLE_INTERVAL_S)
                continue

            live_ports = set(targets.values())
            self._drop_unused(conns, live_ports)

            results = {}
            any_busy = False
            now = time.monotonic()
            for name, port in targets.items():
                if port is None or blocked.get(port, 0.0) > now:
                    continue
                slots = self._fetch(conns, port)
                if slots is None:
                    blocked[port] = now + PORT_BACKOFF_S
                    states.pop(port, None)
                    continue
                blocked.pop(port, None)

                busy = any(bool(s.get("is_processing"))
                           for s in slots if isinstance(s, dict))
                n_decoded, id_task = read_slot_progress(slots)
                state = states.setdefault(port, _PortState())
                speed = self._measure(state, now, n_decoded, id_task, busy)
                if busy and state.measured_task != state.task:
                    # Generating, but this request hasn't produced a measurement
                    # yet. Reporting the previous request's number here would
                    # render it in the live colour as though it were current —
                    # for a reply too short to ever measure, that stale figure
                    # would be all the row ever showed.
                    speed = None
                results[name] = Activity(busy=busy, tok_s=speed, source="slots")
                any_busy = any_busy or busy

            with self._lock:
                self._results = results

            if any_busy and not self._any_busy:
                self.activityStarted.emit()
            self._any_busy = any_busy

            self._stop.wait(SAMPLE_INTERVAL_S)

        self._drop_unused(conns, set())

    @staticmethod
    def _drop_unused(conns: dict, keep: set) -> None:
        for port in [p for p in conns if p not in keep]:
            try:
                conns.pop(port).close()
            except Exception:
                pass

    def _fetch(self, conns: dict, port: int) -> Optional[list]:
        """GET /slots over a kept-alive connection, reconnecting once on error."""
        for attempt in (1, 2):
            conn = conns.get(port)
            if conn is None:
                conn = http.client.HTTPConnection(
                    "127.0.0.1", port, timeout=SAMPLE_TIMEOUT_S)
                conns[port] = conn
            try:
                conn.request("GET", "/slots")
                payload = conn.getresponse().read()
                data = json.loads(payload)
                return data if isinstance(data, list) and data else None
            except Exception:
                # A kept-alive connection the runner closed in the meantime
                # fails exactly once; drop it and let the retry redial.
                try:
                    conn.close()
                except Exception:
                    pass
                conns.pop(port, None)
                if attempt == 2:
                    return None
        return None

    @staticmethod
    def _measure(state: "_PortState", now, n_decoded, id_task, busy):
        """tok/s over the trailing window of samples where tokens actually grew.

        Only points at which the token count *increased* are recorded. Idle
        samples are dropped rather than appended, which matters more than it
        sounds: recording them would keep stretching the measurement span with
        dead time after a request finished, so a genuine 209 tok/s burst decayed
        toward single digits the longer the model then sat idle.

        Readings are normally taken only while the runner reports itself busy.
        The interval in which a generation *ends* is part dead time, so measuring
        across it reads low — that artifact was what froze a real 83.6 tok/s
        request at a displayed 54. The one exception is a request so short that
        no busy interval was ever captured: there the closing sample is the only
        evidence there is, and an approximate number beats a permanent dash for
        the small fast models this sampler exists to cover.
        """
        if n_decoded is None:
            return state.speed

        # n_decoded restarts at 0 for each request, so a new task or a backwards
        # count means the window must start over rather than read as negative.
        if state.task != id_task or (state.samples and n_decoded < state.samples[-1][1]):
            state.reset(id_task)

        if state.samples is None:
            state.samples = deque()

        if not state.samples:
            # Refuse a zero-token baseline. A slot registers its task before the
            # first token exists, so a window anchored there measures prompt
            # processing as though it were generation: a 1.5B model that really
            # runs at ~190 tok/s reported 13.8 after a short prompt, because
            # 10 tokens were divided by 0.7s that was mostly prompt eval.
            # Anchoring at a point where tokens already exist means every
            # interval measured is one the model spent generating.
            if n_decoded > 0:
                state.samples.append((now, n_decoded))
            return state.speed
        if n_decoded <= state.samples[-1][1]:
            return state.speed                       # no new tokens

        if (now - state.samples[-1][0]) > GROWTH_GAP_MAX_S:
            # Generation wasn't continuous across this gap; start over from here
            # rather than averaging the idle time into the rate.
            state.samples.clear()
            state.samples.append((now, n_decoded))
            return state.speed

        state.samples.append((now, n_decoded))
        while len(state.samples) > 2 and (now - state.samples[0][0]) > SPEED_WINDOW_S:
            state.samples.popleft()

        first_seen = state.measured_task == id_task
        if not busy and first_seen:
            return state.speed  # don't let the closing partial interval drag it down

        span = state.samples[-1][0] - state.samples[0][0]
        grown = state.samples[-1][1] - state.samples[0][1]
        if span >= SPEED_MIN_SPAN_S and grown > 0:
            state.speed = grown / span
            state.measured_task = id_task
        return state.speed


class NvmlActivityProbe:
    """Fallback busy signal from per-process GPU utilization.

    Only consulted when a model has no /slots answer — an unmapped runner, or a
    runner whose endpoint isn't serving. Cheap: one NVML call covers every PID.
    """

    def __init__(self, gpu_reader=None):
        self._gpu_reader = gpu_reader

    def probe(self, runner: Optional[Runner]) -> Activity:
        if runner is None:
            return Activity()
        reader = self._gpu_reader
        if reader is None or not hasattr(reader, "process_sm_utilization"):
            return Activity()
        try:
            usage = reader.process_sm_utilization()
        except Exception:
            return Activity()
        if not usage:
            return Activity()
        return Activity(busy=usage.get(runner.pid, 0) >= SM_BUSY_PCT,
                        tok_s=None, source="nvml")
