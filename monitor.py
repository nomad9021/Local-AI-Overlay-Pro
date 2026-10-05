"""
OllamaMonitor — polls Ollama's /api/ps on a QTimer and turns the raw
model-list diff into load/switch/unload/idle signals + GPU stats, so the
UI layer (app.py) never has to poll or diff anything itself.

Beyond the raw resident list it maintains a *roster*: every model worth showing
a row for, each carrying a run state (ACTIVE / WAITING / STOPPED), how long it
has left before Ollama evicts it, and its current generation speed. The roster
is deliberately stickier than /api/ps:

- A model that disappears from /api/ps stays on the roster as STOPPED for
  STOPPED_LINGER_S, so an unload that happens while you're looking away is
  still visible when you come back rather than silently vanishing.
- Models named in OLLAMA_OVERLAY_MODELS are pinned: they always hold a row,
  showing STOPPED before they've ever been loaded. That's what makes a fixed
  three-model working set readable at a glance from a cold start.

State detection is layered (see model_state.py for why): the runner's /slots
`is_processing` flag first, per-process GPU utilization second, and finally an
/api/ps-only fallback that infers a just-finished request from `expires_at`
jumping forward. The last layer needs no extra dependency or endpoint, so
ACTIVE still flashes on completion even in the most degraded configuration.

The /slots layer is sampled continuously by a background thread (SlotsSampler)
rather than on this timer, because generation speed is the growth of a token
counter and a fast model can answer entirely between two UI-rate polls. This
poll loop therefore does no /slots I/O itself: it reads the sampler's latest
snapshot, which also keeps that HTTP work off the UI thread.
"""
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from PySide6.QtCore import QObject, QTimer, Signal

from error_log import log_exception
from gpu_stats import GPUReader, GPUStats
from model_state import ModelState, NvmlActivityProbe, RunnerIndex, SlotsSampler
from token_speed import TokenSpeedTracker

# How long an unloaded model keeps its row before it's dropped entirely.
STOPPED_LINGER_S = 600.0

# After a completion is detected, hold ACTIVE this long so a finished request
# is actually visible on a 1.5s poll cycle instead of flickering for one frame.
ACTIVE_STICKY_S = 3.0

# Polling speeds up while something is generating. tok/s is measured from the
# growth of the runner's decoded-token count between two polls, so the interval
# is also the measurement window: at the idle rate a short reply can begin and
# end inside one interval and never get sampled at all. 300ms keeps the window
# tight enough to measure most real replies while costing only a few
# millisecond-scale localhost requests per second, and only while generating.
POLL_ACTIVE_MS = 300

# Comma-separated model names to always keep a row for, e.g.
#   set OLLAMA_OVERLAY_MODELS=qwen3:8b,hermes4-14b:latest,gemma4:12b
PINNED_ENV_VAR = "OLLAMA_OVERLAY_MODELS"

# Ollama reports a zero-ish timestamp for "no expiry"; anything this old is
# not a real keep_alive deadline.
_MIN_SANE_YEAR = 2000


def parse_expires_at(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        # Older Pythons reject >6 fractional-second digits (Ollama emits 7).
        trimmed = re.sub(r"(\.\d{6})\d+", r"\1", raw).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(trimmed)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.year < _MIN_SANE_YEAR:
        return None
    return parsed


@dataclass
class ModelInfo:
    name: str
    size: int
    size_vram: int
    expires_at: str
    context_length: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "ModelInfo":
        return cls(
            name=d.get("name") or d.get("model", ""),
            size=d.get("size", 0),
            size_vram=d.get("size_vram", 0),
            expires_at=d.get("expires_at", ""),
            context_length=d.get("context_length", 0) or 0,
        )

    @property
    def size_vram_gb(self) -> float:
        return self.size_vram / (1024 ** 3)

    @property
    def expires_dt(self) -> Optional[datetime]:
        return parse_expires_at(self.expires_at)


@dataclass
class ModelStatus:
    """One roster row: what the HUD needs to draw a model, state included."""
    name: str
    state: ModelState = ModelState.STOPPED
    size_vram_gb: float = 0.0
    seconds_left: Optional[float] = None   # until Ollama evicts it (WAITING)
    tok_s: Optional[float] = None
    source: str = "none"                   # which detection layer answered
    pinned: bool = False
    stopped_at: Optional[float] = None     # monotonic time it left /api/ps

    @property
    def is_resident(self) -> bool:
        return self.state is not ModelState.STOPPED


_STATE_ORDER = {ModelState.ACTIVE: 0, ModelState.WAITING: 1, ModelState.STOPPED: 2}


def sort_roster(rows) -> list:
    """Roster rows ordered active first, then waiting, then stopped."""
    return sorted(rows, key=lambda s: (_STATE_ORDER.get(s.state, 3), s.name.lower()))


# How often the server version is re-read while Ollama is reachable.
VERSION_REFRESH_S = 300.0


class OllamaMonitor(QObject):
    # list[ModelStatus], GPUStats — emitted every poll tick with the current roster
    rosterChanged = Signal(list, object)
    modelLoaded = Signal(list)     # names added, no previous models
    modelSwitched = Signal(list, list)  # names removed, names added
    modelUnloaded = Signal(list)   # names removed, other models still active
    becameActive = Signal()        # nothing to show -> at least one row
    becameIdle = Signal()          # last row aged out of the roster

    def __init__(self, gpu_reader: GPUReader, base_url="http://localhost:11434",
                 poll_ms=1500, pinned=None, parent=None):
        super().__init__(parent)
        self._gpu_reader = gpu_reader
        self._base_url = base_url
        self._poll_ms = poll_ms
        self._session = requests.Session()
        self._prev: dict[str, ModelInfo] = {}
        self._had_rows = False

        self._pinned = list(pinned) if pinned is not None else self._pinned_from_env()
        self._roster: dict[str, ModelStatus] = {
            name: ModelStatus(name=name, pinned=True) for name in self._pinned
        }
        self._prev_expiry: dict[str, datetime] = {}
        self._last_completion: dict[str, float] = {}
        self._was_generating = False

        self._runner_index = RunnerIndex(base_url)
        self._sampler = SlotsSampler(self)
        self._nvml = NvmlActivityProbe(gpu_reader=gpu_reader)
        # A generation can begin between poll ticks; the sampler notices within
        # ~100ms, so let it pull the roster forward instead of the HUD lagging.
        self._sampler.activityStarted.connect(self._poll)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

        # Kept as a last-resort speed source for older Ollama builds that still
        # log `slot print_timing`; 0.30.10 doesn't, so /slots normally wins.
        self._speed_tracker = TokenSpeedTracker(self)
        self._speed_tracker.speedUpdated.connect(self._on_log_speed)
        self._log_speed: dict[str, float] = {}

        # Reachability + version, read by the dashboard and the sharing server.
        self.ollama_online = False
        self.ollama_version = ""
        self._version_checked_at = -1e9

    @property
    def base_url(self) -> str:
        return self._base_url

    @staticmethod
    def _pinned_from_env() -> list:
        raw = os.environ.get(PINNED_ENV_VAR, "")
        return [name.strip() for name in raw.split(",") if name.strip()]

    @property
    def pinned(self) -> list:
        return list(self._pinned)

    def set_pinned(self, names: list) -> None:
        """Change the pinned set live: new pins get a row immediately, dropped
        pins become ordinary rows that age out like any other."""
        names = [n for n in names if n]
        for name in names:
            status = self._roster.get(name)
            if status is None:
                self._roster[name] = ModelStatus(name=name, pinned=True,
                                                 stopped_at=time.monotonic())
            else:
                status.pinned = True
        for name, status in self._roster.items():
            if name not in names:
                status.pinned = False
        self._pinned = names

    def set_poll_interval(self, ms: int) -> None:
        self._poll_ms = max(250, int(ms))
        if self._timer.isActive() and self._timer.interval() != POLL_ACTIVE_MS:
            self._timer.setInterval(self._poll_ms)

    def start(self):
        self._timer.start(self._poll_ms)
        self._sampler.start()
        self._speed_tracker.start()
        self._poll()

    def stop(self):
        self._timer.stop()
        self._sampler.stop()
        self._speed_tracker.stop()
        self._gpu_reader.shutdown()

    def _on_log_speed(self, name: str, tok_s: float):
        self._log_speed[name] = tok_s

    def _poll(self):
        try:
            resp = self._session.get(f"{self._base_url}/api/ps", timeout=1.0)
            resp.raise_for_status()
            raw_models = resp.json().get("models", [])
            models = {m.name: m for m in (ModelInfo.from_dict(d) for d in raw_models)}
            self.ollama_online = True
        except Exception:
            models = {}
            self.ollama_online = False

        if self.ollama_online and (time.monotonic() - self._version_checked_at) > VERSION_REFRESH_S:
            self._version_checked_at = time.monotonic()
            try:
                resp = self._session.get(f"{self._base_url}/api/version", timeout=1.0)
                self.ollama_version = resp.json().get("version", "") or ""
            except Exception:
                pass

        try:
            self._speed_tracker.set_active_models(list(models))
            self._update_roster(models)
            self._transition(models)
        except Exception:
            # _update_roster/_transition emit signals into app.py's slots (fade_in,
            # set_roster, show_toast). An uncaught exception from any of those would
            # otherwise propagate out of this QTimer slot and, under pythonw.exe (no
            # console, sys.stderr=None — how the Startup-folder autostart runs), can
            # silently stop this timer from ever firing again: the process keeps
            # running, but model-load detection dies permanently with no visible
            # error. The next tick retries from scratch since self._prev is only
            # updated once a transition fully completes, so a one-off failure
            # self-heals.
            log_exception("OllamaMonitor._poll transition")

    def _detect_completions(self, models: dict, now: float) -> None:
        """Record models whose keep_alive deadline just moved forward.

        Verified on Ollama 0.30.10: `expires_at` is frozen for the whole duration
        of a request and jumps to now + keep_alive the moment it completes. That
        makes a forward jump an exact "this model just finished generating"
        signal, available from /api/ps alone.
        """
        for name, info in models.items():
            expiry = info.expires_dt
            if expiry is None:
                continue
            previous = self._prev_expiry.get(name)
            if previous is not None and expiry > previous:
                self._last_completion[name] = now
            self._prev_expiry[name] = expiry

        for name in list(self._prev_expiry):
            if name not in models:
                del self._prev_expiry[name]

    def _update_roster(self, models: dict) -> None:
        now = time.monotonic()
        wall_now = datetime.now(timezone.utc)

        self._detect_completions(models, now)

        resident = list(models.values())
        try:
            self._runner_index.refresh(resident, now)
        except Exception:
            log_exception("RunnerIndex.refresh")

        # Tell the sampler which runner belongs to which model, then read what
        # it has already collected. Both are cheap dict swaps under one lock.
        runners = {m.name: self._runner_index.runner_for(m.name, m.context_length or None)
                   for m in resident}
        self._sampler.set_targets({name: runner.port
                                   for name, runner in runners.items()
                                   if runner is not None and runner.port is not None})
        sampled = self._sampler.snapshot()

        for name, info in models.items():
            status = self._roster.get(name)
            if status is None:
                status = ModelStatus(name=name)
                self._roster[name] = status

            activity = sampled.get(name)
            if activity is None or activity.busy is None:
                activity = self._nvml.probe(runners.get(name))

            just_finished = (now - self._last_completion.get(name, -1e9)) <= ACTIVE_STICKY_S
            busy = bool(activity.busy) or just_finished

            status.state = ModelState.ACTIVE if busy else ModelState.WAITING
            status.size_vram_gb = info.size_vram_gb
            status.stopped_at = None
            status.source = activity.source if activity.busy is not None else (
                "expiry" if just_finished else "none"
            )

            expiry = info.expires_dt
            status.seconds_left = (
                max(0.0, (expiry - wall_now).total_seconds()) if expiry else None
            )

            if activity.source == "slots":
                # The sampler is authoritative when it answered: None means
                # "generating, nothing measured for this request yet", which the
                # row must show as blank rather than as a stale live figure.
                status.tok_s = activity.tok_s
            else:
                # Coarser layers can't measure speed at all, so hold whatever
                # was last measured rather than blanking a good number.
                fallback = activity.tok_s or self._log_speed.get(name)
                if fallback is not None:
                    status.tok_s = fallback

        for name, status in list(self._roster.items()):
            if name in models:
                continue
            if status.state is not ModelState.STOPPED:
                status.state = ModelState.STOPPED
                status.stopped_at = now
                status.seconds_left = None
                status.size_vram_gb = 0.0
            if status.pinned:
                continue
            aged = status.stopped_at is not None and (now - status.stopped_at) > STOPPED_LINGER_S
            if aged:
                del self._roster[name]

        gpu = self._gpu_reader.read() or GPUStats(available=False)
        rows = self.roster()
        self._apply_poll_rate(rows)
        self.rosterChanged.emit(rows, gpu)

        # becameActive/becameIdle track "is there anything to show", which is the
        # roster, not /api/ps — otherwise the HUD would fade out the instant a
        # model unloaded and take the new STOPPED row with it.
        has_rows = bool(rows)
        if has_rows and not self._had_rows:
            self.becameActive.emit()
        elif not has_rows and self._had_rows:
            self.becameIdle.emit()
        self._had_rows = has_rows

    def _apply_poll_rate(self, rows: list) -> None:
        generating = any(s.state is ModelState.ACTIVE for s in rows)
        wanted = POLL_ACTIVE_MS if generating else self._poll_ms
        if self._timer.interval() != wanted:
            self._timer.setInterval(wanted)

        self._was_generating = generating

    def roster(self) -> list:
        """Roster rows ordered active first, then waiting, then stopped."""
        return sort_roster(self._roster.values())

    def _transition(self, new_models: dict) -> None:
        prev_names = set(self._prev)
        new_names = set(new_models)

        if prev_names == new_names:
            self._prev = new_models
            return

        if not prev_names and new_names:
            self.modelLoaded.emit(list(new_names))
        elif prev_names and not new_names:
            self.modelUnloaded.emit(list(prev_names))
        else:
            added = new_names - prev_names
            removed = prev_names - new_names
            if added and removed:
                self.modelSwitched.emit(list(removed), list(added))
            elif added:
                self.modelLoaded.emit(list(added))
            elif removed:
                self.modelUnloaded.emit(list(removed))

        self._prev = new_models
