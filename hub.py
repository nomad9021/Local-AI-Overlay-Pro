"""
StatusHub — the single place the HUD and the dashboard read host status from.

The local OllamaMonitor and the RemotePoller both feed it; it keeps the latest
HostStatus per host and turns successive statuses into the events worth a toast
(a host going offline/online, a remote model loading or unloading). Local model
transitions keep coming from OllamaMonitor's own signals, which already have
the right load/switch/unload semantics.
"""
import time

from PySide6.QtCore import QObject, Signal

from gpu_stats import GPUStats
from remote import HostStatus
from settings import LOCAL_ID, MODE_AGENT


class StatusHub(QObject):
    updated = Signal(str)                      # host_id
    removed = Signal(str)                      # host_id
    reachabilityChanged = Signal(str, bool)    # host_id, reachable now
    remoteModelsChanged = Signal(str, list, list)   # host_id, loaded, unloaded

    def __init__(self, parent=None):
        super().__init__(parent)
        self._status: dict = {}

    def status(self, host_id: str):
        return self._status.get(host_id)

    def all(self) -> dict:
        return dict(self._status)

    def all_rows(self) -> list:
        rows = []
        for status in self._status.values():
            if status.reachable:
                rows.extend(status.rows)
        return rows

    def set_local(self, rows: list, gpu, ollama_online: bool, ollama_version: str,
                  url: str, name: str) -> None:
        self._status[LOCAL_ID] = HostStatus(
            host_id=LOCAL_ID,
            reachable=True,   # the local machine is always "reachable"; Ollama may not be
            ollama_online=ollama_online,
            error="" if ollama_online else "Ollama isn't running",
            latency_ms=None,
            name=name,
            ollama_version=ollama_version,
            mode=MODE_AGENT,
            url=url,
            rows=list(rows),
            gpu=gpu or GPUStats(available=False),
            last_seen=time.time(),
        )
        self.updated.emit(LOCAL_ID)

    def set_remote(self, host_id: str, status: HostStatus) -> None:
        previous = self._status.get(host_id)
        self._status[host_id] = status
        if previous is not None:
            if previous.reachable != status.reachable:
                self.reachabilityChanged.emit(host_id, status.reachable)
            elif status.reachable and previous.reachable:
                loaded = sorted(status.resident_names - previous.resident_names)
                unloaded = sorted(previous.resident_names - status.resident_names)
                if loaded or unloaded:
                    self.remoteModelsChanged.emit(host_id, loaded, unloaded)
        self.updated.emit(host_id)

    def forget(self, host_id: str) -> None:
        if self._status.pop(host_id, None) is not None:
            self.removed.emit(host_id)
