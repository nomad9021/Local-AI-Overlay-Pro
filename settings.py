"""
Persistent settings, shared by the HUD, the remote poller, the sharing server
and the settings window.

Stored as JSON in the per-user config dir (platform_support.config_dir()):
  Windows: %APPDATA%\\LocalAIOverlayPro\\config.json
  Linux:   ~/.config/local-ai-overlay-pro/config.json

The file is deep-merged over DEFAULTS on load, so a config written by an older
version gains new keys automatically and a hand-edited file missing a key
doesn't crash anything. Writes are atomic (temp file + replace) so a crash
mid-save can't leave a truncated config behind.
"""
import copy
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from error_log import log_exception
from platform_support import config_dir, hostname
from theme import ACCENT, auto_tag_colour, valid_colour

LOCAL_ID = "local"

MODE_AGENT = "agent"     # another machine running agent.py / sharing enabled
MODE_OLLAMA = "ollama"   # a bare Ollama API (OLLAMA_HOST=0.0.0.0 on that box)

KIND_COMPUTER = "computer"
KIND_SERVER = "server"

DEFAULTS = {
    "version": 2,
    "ollama_url": "http://127.0.0.1:11434",
    "poll_interval_ms": 1500,
    "pinned_models": [],
    "local": {
        "name": "",            # blank -> this machine's hostname
        "color": ACCENT,
        "tags": [],
        "show": True,
    },
    "remote_poll_interval_s": 3,
    "hosts": [],
    "tags": {
        "gpu": "#F472B6",
        "prod": "#F87171",
        "lab": "#34D399",
        "dev": "#FBBF24",
        "backup": "#94A3B8",
    },
    "appearance": {
        "state_active": "#4ADE80",
        "state_waiting": "#FBBF24",
        "state_stopped": "#8b8b93",
        "kind_computer": "#A78BFA",
        "kind_server": "#60A5FA",
        "kind_local": "#4ADE80",
        "width": 272,
        "opacity": 175,          # card background alpha, 0-255
        "border_width": 1,
        "border_radius": 14,
        "corner": "top-right",   # top-right | top-left | bottom-right | bottom-left
        "layout": "vertical",    # vertical | horizontal
        "max_rows": 6,
        "show_gpu": True,
    },
    "notifications": {
        "toasts": True,
        "remote_model_toasts": False,
        "host_offline_toasts": True,
        "toast_seconds": 4,
    },
    "behavior": {
        "auto_hide_when_idle": True,
    },
    "sharing": {
        "enabled": False,
        "bind": "0.0.0.0",
        "port": 11480,
        "token": "",
        "beacon": True,
    },
}


@dataclass
class HostConfig:
    """One remote machine the user wants to watch."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = "New host"
    address: str = ""
    mode: str = MODE_AGENT
    kind: str = KIND_COMPUTER
    color: str = "#60A5FA"
    tags: list = field(default_factory=list)
    token: str = ""
    pinned_models: list = field(default_factory=list)
    enabled: bool = True
    show_in_overlay: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "HostConfig":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in (d or {}).items() if k in known}
        host = cls(**clean)
        host.tags = [str(t).strip() for t in (host.tags or []) if str(t).strip()]
        host.pinned_models = [str(m).strip() for m in (host.pinned_models or []) if str(m).strip()]
        if host.mode not in (MODE_AGENT, MODE_OLLAMA):
            host.mode = MODE_AGENT
        if host.kind not in (KIND_COMPUTER, KIND_SERVER):
            host.kind = KIND_COMPUTER
        host.color = valid_colour(host.color, "#60A5FA")
        return host

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Identity:
    """How a host is labelled everywhere it's drawn (HUD card, dashboard tile)."""
    name: str
    color: str
    kind: str
    kind_color: str
    tags: list   # [(text, colour)]


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "tags":
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def split_list(text: str) -> list:
    return [part.strip() for part in (text or "").replace(";", ",").split(",") if part.strip()]


class Settings(QObject):
    changed = Signal()

    def __init__(self, path: Path = None, parent=None):
        super().__init__(parent)
        self.path = path or (config_dir() / "config.json")
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    # ---- persistence ---------------------------------------------------- #

    def load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = _deep_merge(DEFAULTS, raw)
        except Exception:
            log_exception("Settings.load")
            self.data = copy.deepcopy(DEFAULTS)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            log_exception("Settings.save")

    def replace(self, data: dict) -> None:
        """Adopt a whole edited copy (from the settings window), save, notify."""
        self.data = _deep_merge(DEFAULTS, data)
        self.save()
        self.changed.emit()

    def snapshot(self) -> dict:
        return copy.deepcopy(self.data)

    # ---- typed accessors ------------------------------------------------ #

    def section(self, name: str) -> dict:
        return self.data.get(name) or {}

    def hosts(self) -> list:
        return [HostConfig.from_dict(h) for h in self.data.get("hosts", [])]

    def host(self, host_id: str):
        for h in self.hosts():
            if h.id == host_id:
                return h
        return None

    def pinned_models(self) -> list:
        """Settings pins plus the legacy OLLAMA_OVERLAY_MODELS env var."""
        pins = list(self.data.get("pinned_models") or [])
        for name in split_list(os.environ.get("OLLAMA_OVERLAY_MODELS", "")):
            if name not in pins:
                pins.append(name)
        return pins

    def tag_colour(self, tag: str) -> str:
        colours = self.data.get("tags") or {}
        for name, colour in colours.items():
            if name.lower() == tag.lower():
                return valid_colour(colour, auto_tag_colour(tag))
        return auto_tag_colour(tag)

    def local_name(self) -> str:
        return (self.section("local").get("name") or "").strip() or hostname()

    def identity(self, host_id: str) -> Identity:
        look = self.section("appearance")
        if host_id == LOCAL_ID:
            local = self.section("local")
            tags = [(t, self.tag_colour(t)) for t in local.get("tags") or []]
            return Identity(
                name=self.local_name(),
                color=valid_colour(local.get("color"), ACCENT),
                kind="local",
                kind_color=valid_colour(look.get("kind_local"), ACCENT),
                tags=tags,
            )
        host = self.host(host_id)
        if host is None:
            return Identity(host_id, "#6B7280", "", "#6B7280", [])
        return Identity(
            name=host.name or host.address,
            color=host.color,
            kind=host.kind,
            kind_color=valid_colour(look.get(f"kind_{host.kind}"), "#60A5FA"),
            tags=[(t, self.tag_colour(t)) for t in host.tags],
        )
