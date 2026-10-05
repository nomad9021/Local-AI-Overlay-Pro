"""
Watching Ollama on other machines.

Each configured host (settings.HostConfig) is polled on a background thread and
reported as a HostStatus — the same shape the local monitor's output is wrapped
in, so the HUD and the dashboard draw local and remote machines identically.

Two host modes:

- "agent": the machine runs agent.py (or an overlay with Sharing on). Its
  /api/v1/status carries the full local picture — ACTIVE/WAITING/STOPPED from
  the runner's /slots, live tok/s, GPU%/VRAM — so a remote row is exactly as
  truthful as a local one.
- "ollama": a bare Ollama API, reachable because that server was started with
  OLLAMA_HOST=0.0.0.0. Only /api/ps is available, so state comes from the
  `expires_at` layer (see model_state.py): resident models show WAITING with
  their eviction countdown, ACTIVE flashes when a request completes, and there
  is no tok/s or GPU data. Good for servers you can't install anything on.

Hosts are polled concurrently, so one dead or slow host never delays the rest;
each request has a short timeout and a failure just marks that host offline
until the next round.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import requests
from PySide6.QtCore import QObject, Signal

from discovery import DEFAULT_AGENT_PORT, DEFAULT_OLLAMA_PORT
from error_log import log_exception
from gpu_stats import GPUStats
from model_state import ModelState
from monitor import ACTIVE_STICKY_S, STOPPED_LINGER_S, ModelInfo, ModelStatus, sort_roster
from settings import MODE_AGENT, MODE_OLLAMA, HostConfig

CONNECT_TIMEOUT_S = 2.0
READ_TIMEOUT_S = 3.0


@dataclass
class HostStatus:
    host_id: str
    reachable: bool = False          # could we talk to the host/agent at all
    ollama_online: bool = False      # is Ollama itself answering on that host
    error: str = ""
    latency_ms: Optional[float] = None
    name: str = ""
    hostname: str = ""
    platform: str = ""
    ollama_version: str = ""
    agent_version: str = ""
    mode: str = MODE_AGENT
    url: str = ""
    rows: list = field(default_factory=list)
    gpu: GPUStats = field(default_factory=lambda: GPUStats(available=False))
    last_seen: Optional[float] = None   # wall-clock time of the last good answer
    checked_at: float = field(default_factory=time.time)

    @property
    def resident_names(self) -> set:
        return {s.name for s in self.rows if s.state is not ModelState.STOPPED}


def normalize_url(address: str, mode: str) -> str:
    """'10.0.0.5', '10.0.0.5:11434', 'http://box:11480/' -> 'http://host:port'."""
    text = (address or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        port = parts.port or (DEFAULT_AGENT_PORT if mode == MODE_AGENT else DEFAULT_OLLAMA_PORT)
    except ValueError:
        return ""
    if not host:
        return ""
    if ":" in host:
        host = f"[{host}]"   # IPv6 literal
    scheme = parts.scheme if parts.scheme in ("http", "https") else "http"
    return f"{scheme}://{host}:{port}"


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "timed out connecting"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "timed out waiting for reply"
    if isinstance(exc, requests.exceptions.ConnectionError):
        text = str(exc).lower()
        if "refused" in text or "10061" in text:
            return "connection refused (nothing listening on that port)"
        if "name or service" in text or "getaddrinfo" in text or "nodename" in text:
            return "hostname not found"
        return "unreachable"
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        if exc.response.status_code == 401:
            return "token rejected (401) — check the host's token"
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, ValueError):
        return "unexpected reply (is this the right port/mode?)"
    return str(exc) or exc.__class__.__name__


class _DirectRoster:
    """Roster bookkeeping for a bare-Ollama host, mirroring OllamaMonitor's
    rules (STOPPED linger, pinned rows, completion detection via expires_at)
    without the runner-level layers that only work on the machine itself."""

    def __init__(self):
        self.prev_expiry: dict = {}
        self.last_completion: dict = {}
        self.roster: dict = {}

    def update(self, raw_models: list, pinned: list) -> list:
        now = time.monotonic()
        wall_now = datetime.now(timezone.utc)
        models = {m.name: m for m in (ModelInfo.from_dict(d) for d in raw_models)}

        for name in pinned:
            self.roster.setdefault(name, ModelStatus(name=name, pinned=True))
        for name, status in self.roster.items():
            status.pinned = name in pinned

        for name, info in models.items():
            expiry = info.expires_dt
            previous = self.prev_expiry.get(name)
            if expiry is not None:
                if previous is not None and expiry > previous:
                    self.last_completion[name] = now
                self.prev_expiry[name] = expiry

            status = self.roster.setdefault(name, ModelStatus(name=name))
            just_finished = (now - self.last_completion.get(name, -1e9)) <= ACTIVE_STICKY_S
            status.state = ModelState.ACTIVE if just_finished else ModelState.WAITING
            status.source = "expiry" if just_finished else "none"
            status.size_vram_gb = info.size_vram_gb
            status.stopped_at = None
            status.seconds_left = (max(0.0, (expiry - wall_now).total_seconds())
                                   if expiry else None)

        for name in [n for n in self.prev_expiry if n not in models]:
            del self.prev_expiry[name]

        for name, status in list(self.roster.items()):
            if name in models:
                continue
            if status.state is not ModelState.STOPPED:
                status.state = ModelState.STOPPED
                status.stopped_at = now
                status.seconds_left = None
                status.size_vram_gb = 0.0
            if status.pinned:
                continue
            if status.stopped_at is not None and (now - status.stopped_at) > STOPPED_LINGER_S:
                del self.roster[name]

        return sort_roster(self.roster.values())


def _rows_from_agent(models: list) -> list:
    rows = []
    for m in models or []:
        try:
            state = ModelState(m.get("state", "STOPPED"))
        except ValueError:
            state = ModelState.STOPPED
        rows.append(ModelStatus(
            name=str(m.get("name", "?")),
            state=state,
            size_vram_gb=float(m.get("size_vram_gb") or 0.0),
            seconds_left=m.get("seconds_left"),
            tok_s=m.get("tok_s"),
            source=str(m.get("source") or "none"),
            pinned=bool(m.get("pinned")),
        ))
    return sort_roster(rows)


def _gpu_from_agent(g: dict) -> GPUStats:
    g = g or {}
    return GPUStats(
        name=str(g.get("name") or "GPU"),
        gpu_pct=float(g.get("gpu_pct") or 0.0),
        vram_used_gb=float(g.get("vram_used_gb") or 0.0),
        vram_total_gb=float(g.get("vram_total_gb") or 0.0),
        available=bool(g.get("available")),
    )


def poll_host(host: HostConfig, session: requests.Session = None,
              roster: _DirectRoster = None, previous: HostStatus = None) -> HostStatus:
    """One request round against one host. Never raises."""
    session = session or requests.Session()
    url = normalize_url(host.address, host.mode)
    status = HostStatus(host_id=host.id, mode=host.mode, url=url, name=host.name)
    if previous is not None:
        status.last_seen = previous.last_seen
    if not url:
        status.error = "no address set"
        return status

    headers = {"Authorization": f"Bearer {host.token}"} if host.token else {}
    timeout = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S)
    started = time.perf_counter()
    try:
        if host.mode == MODE_AGENT:
            resp = session.get(f"{url}/api/v1/status", headers=headers, timeout=timeout)
            resp.raise_for_status()
            body = resp.json()
            status.latency_ms = (time.perf_counter() - started) * 1000
            ollama = body.get("ollama") or {}
            status.reachable = True
            status.ollama_online = bool(ollama.get("online"))
            status.ollama_version = str(ollama.get("version") or "")
            status.hostname = str(body.get("hostname") or "")
            status.platform = str(body.get("platform") or "")
            status.agent_version = str(body.get("agent_version") or "")
            status.rows = _rows_from_agent(body.get("models"))
            status.gpu = _gpu_from_agent(body.get("gpu"))
            if not status.ollama_online:
                status.error = "Ollama not running on that host"
        else:
            resp = session.get(f"{url}/api/ps", headers=headers, timeout=timeout)
            resp.raise_for_status()
            raw = resp.json().get("models", [])
            status.latency_ms = (time.perf_counter() - started) * 1000
            status.reachable = True
            status.ollama_online = True
            status.rows = (roster or _DirectRoster()).update(raw, host.pinned_models)
            if previous is not None and previous.ollama_version:
                status.ollama_version = previous.ollama_version
            else:
                try:
                    v = session.get(f"{url}/api/version", headers=headers, timeout=timeout)
                    status.ollama_version = str(v.json().get("version") or "")
                except Exception:
                    pass
        status.last_seen = time.time()
    except Exception as exc:
        status.error = _describe_error(exc)
    return status


class RemotePoller(QObject):
    """Polls every enabled remote host concurrently on a background thread."""

    statusUpdated = Signal(str, object)   # host_id, HostStatus
    hostRemoved = Signal(str)

    MAX_WORKERS = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._hosts: dict = {}
        self._interval = 3.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sessions: dict = {}
        self._rosters: dict = {}
        self._last: dict = {}

    def set_hosts(self, hosts: list) -> None:
        wanted = {h.id: h for h in hosts if h.enabled}
        with self._lock:
            removed = [hid for hid in self._hosts if hid not in wanted]
            changed = [hid for hid, h in wanted.items()
                       if hid in self._hosts and (self._hosts[hid].address, self._hosts[hid].mode)
                       != (h.address, h.mode)]
            self._hosts = wanted
            for hid in removed + changed:
                self._rosters.pop(hid, None)
                self._last.pop(hid, None)
                session = self._sessions.pop(hid, None)
                if session is not None:
                    session.close()
        for hid in removed:
            self.hostRemoved.emit(hid)
        self._wake.set()

    def set_interval(self, seconds: float) -> None:
        self._interval = max(1.0, float(seconds))

    def poll_now(self) -> None:
        self._wake.set()

    def latest(self) -> dict:
        with self._lock:
            return dict(self._last)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="remote-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _poll_one(self, host: HostConfig) -> HostStatus:
        with self._lock:
            session = self._sessions.setdefault(host.id, requests.Session())
            roster = self._rosters.setdefault(host.id, _DirectRoster())
            previous = self._last.get(host.id)
        return poll_host(host, session=session, roster=roster, previous=previous)

    def _run(self) -> None:
        pool = ThreadPoolExecutor(max_workers=self.MAX_WORKERS, thread_name_prefix="remote")
        try:
            while not self._stop.is_set():
                with self._lock:
                    hosts = list(self._hosts.values())
                futures = {pool.submit(self._poll_one, h): h for h in hosts}
                deadline = CONNECT_TIMEOUT_S + READ_TIMEOUT_S + 3.0
                for future, host in futures.items():
                    try:
                        status = future.result(timeout=deadline)
                    except FutureTimeout:
                        status = HostStatus(host_id=host.id, error="poll timed out")
                    except Exception:
                        log_exception("RemotePoller._run")
                        continue
                    with self._lock:
                        if host.id not in self._hosts:
                            continue   # removed while this request was in flight
                        self._last[host.id] = status
                    self.statusUpdated.emit(host.id, status)
                self._wake.wait(self._interval)
                self._wake.clear()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            for session in self._sessions.values():
                session.close()
