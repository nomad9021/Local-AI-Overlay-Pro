"""
Settings & dashboard window.

Tabs:
  Dashboard  — every host (this machine + remotes) as a live card in a grid,
               with address, platform, versions, latency and last-seen.
  Hosts      — add/edit/remove the computers and servers to watch: address,
               mode (agent / bare Ollama API), kind, border colour, tags,
               token; "Test" probes one host, "Discover" finds them on the LAN.
  Tags       — the tag palette: each tag's colour, used on every card.
  Appearance — state colours, kind-badge colours, card width/opacity/border,
               HUD corner and stacking direction.
  General    — this machine's card, Ollama URL and polling, pinned models,
               notifications, start-at-login.
  Sharing    — the passthrough server that makes this machine visible to
               overlays elsewhere.

Edits go into a draft copy of the settings; Apply/Save hand the whole draft to
Settings.replace(), which persists it and notifies the HUD, the poller and the
sharing server so everything updates live. Start-at-login is the exception: it
changes OS state, so it's applied the moment the box is ticked.
"""
import copy
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSlider,
    QSpinBox, QTableWidget, QTabWidget, QVBoxLayout, QWidget,
)

import platform_support
from discovery import DEFAULT_AGENT_PORT, discover, local_ipv4
from error_log import _LOG_PATH
from model_state import ModelState
from platform_support import APP_NAME, APP_VERSION
from remote import normalize_url, poll_host
from settings import (
    KIND_COMPUTER, KIND_SERVER, LOCAL_ID, MODE_AGENT, MODE_OLLAMA, HostConfig, split_list,
)
from status_server import generate_token
from theme import ACCENT, DANGER, LABEL_GRAY, auto_tag_colour, pill_style, valid_colour
from widgets import HostCard, status_text_for

HOST_PALETTE = ["#60A5FA", "#F472B6", "#A78BFA", "#34D399", "#FBBF24",
                "#F87171", "#22D3EE", "#FB923C", "#E879F9", "#A3E635"]

REPO_RAW = "https://raw.githubusercontent.com/nomad9021/Local-AI-Overlay-Pro/main/install"

STYLE = f"""
QWidget#settingsRoot {{ background: #141417; }}
QTabWidget::pane {{ border: 1px solid #2a2a2e; border-radius: 8px; top: -1px; }}
QTabBar::tab {{ background: #1a1a1e; color: #b5b5bb; padding: 8px 16px;
               border: 1px solid #2a2a2e; border-bottom: none;
               border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: #232328; color: {ACCENT}; }}
QGroupBox {{ border: 1px solid #2a2a2e; border-radius: 8px; margin-top: 14px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {LABEL_GRAY}; }}
QLineEdit, QComboBox {{ background: #1c1c20; border: 1px solid #34343a;
                       border-radius: 5px; padding: 4px 6px; min-height: 18px; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QSpinBox {{ min-height: 22px; }}
QPushButton {{ background: #26262b; border: 1px solid #3a3a40; border-radius: 6px; padding: 6px 12px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #5a5a60; }}
QPushButton#primary {{ background: #1f3d2b; border-color: #2f855a; color: white; }}
QListWidget, QTableWidget {{ background: #18181c; border: 1px solid #2a2a2e; border-radius: 6px; }}
QListWidget::item {{ padding: 6px; }}
QListWidget::item:selected {{ background: #23402f; color: white; }}
QHeaderView::section {{ background: #1f1f24; color: {LABEL_GRAY}; border: none; padding: 5px; }}
QScrollArea {{ border: none; background: transparent; }}
QLabel#hint {{ color: {LABEL_GRAY}; }}
"""


def swatch_icon(colour: str, size: int = 14) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(colour))
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pixmap)


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


class _Relay(QObject):
    """Carries a worker thread's result back to the UI thread."""
    done = Signal(object)


def run_in_thread(fn, on_done):
    relay = _Relay()
    relay.done.connect(on_done)

    def work():
        try:
            result = fn()
        except Exception as exc:  # reported to the caller, which shows it
            result = exc
        relay.done.emit(result)

    threading.Thread(target=work, daemon=True).start()
    return relay  # caller keeps it referenced until done fires


class ColorButton(QPushButton):
    colorChanged = Signal(str)

    def __init__(self, colour: str = ACCENT, parent=None):
        super().__init__(parent)
        self._colour = colour
        self.setFixedWidth(110)
        self.clicked.connect(self._pick)
        self._render()

    def colour(self) -> str:
        return self._colour

    def set_colour(self, colour: str):
        self._colour = valid_colour(colour, ACCENT)
        self._render()

    def _render(self):
        self.setIcon(swatch_icon(self._colour, 16))
        self.setText(self._colour.upper())

    def _pick(self):
        chosen = QColorDialog.getColor(QColor(self._colour), self.window(), "Pick a colour")
        if chosen.isValid():
            self.set_colour(chosen.name())
            self.colorChanged.emit(self._colour)


def tag_preview(tags: list, colour_for) -> QWidget:
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)
    for tag in tags:
        pill = QLabel(tag)
        pill.setStyleSheet(pill_style(colour_for(tag)))
        row.addWidget(pill)
    row.addStretch()
    return box


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

def _ago(ts) -> str:
    if not ts:
        return "never"
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    return f"{delta // 3600}h ago"


class DashboardPage(QWidget):
    TILE_WIDTH = 300

    def __init__(self, settings, hub, poller, controller, overlay):
        super().__init__()
        self._settings = settings
        self._hub = hub
        self._poller = poller
        self._tiles: dict = {}
        self._columns = 0

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setStyleSheet("font-size: 11pt;")
        top.addWidget(self.summary, 1)
        refresh = QPushButton("Refresh now")
        refresh.clicked.connect(poller.poll_now)
        unload = QPushButton("Unload local models")
        unload.clicked.connect(controller.unload_async)
        show = QPushButton("Show overlay")
        show.clicked.connect(overlay.restore_from_tray)
        for btn in (refresh, unload, show):
            top.addWidget(btn)
        layout.addLayout(top)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(12)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.grid_host)
        layout.addWidget(self.scroll, 1)
        layout.addWidget(hint(
            "Cards are bordered in each host's colour. Add machines on the Hosts tab; "
            "run the agent on a server (see the Sharing tab) for full detail — states, tok/s and GPU."
        ))

        hub.updated.connect(self._on_updated)
        hub.removed.connect(lambda _h: self.rebuild())
        settings.changed.connect(self.rebuild)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self.rebuild()

    def _host_ids(self) -> list:
        return [LOCAL_ID] + [h.id for h in self._settings.hosts() if h.enabled]

    def rebuild(self):
        wanted = self._host_ids()
        for host_id in [h for h in self._tiles if h not in wanted]:
            tile = self._tiles.pop(host_id)
            self.grid.removeWidget(tile)
            tile.deleteLater()
        look = self._settings.section("appearance")
        for host_id in wanted:
            tile = self._tiles.get(host_id)
            if tile is None:
                tile = HostCard(host_id, width=self.TILE_WIDTH)
                self._tiles[host_id] = tile
            ident = self._settings.identity(host_id)
            tile.set_identity(ident.name, ident.color, ident.kind, ident.kind_color, ident.tags)
            tile.set_appearance(self.TILE_WIDTH, max(1, int(look.get("border_width", 1))),
                                int(look.get("border_radius", 14)), 235, 12, True)
            self._on_updated(host_id, relayout=False)
        self._columns = 0
        self._relayout()

    def _relayout(self):
        columns = max(1, (self.scroll.viewport().width() - 12) // (self.TILE_WIDTH + 12))
        if columns == self._columns:
            return
        self._columns = columns
        for tile in self._tiles.values():
            self.grid.removeWidget(tile)
        for index, host_id in enumerate(self._host_ids()):
            tile = self._tiles.get(host_id)
            if tile is not None:
                self.grid.addWidget(tile, index // columns, index % columns, Qt.AlignTop)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    def showEvent(self, event):
        super().showEvent(event)
        self._columns = 0
        QTimer.singleShot(0, self._relayout)

    def _on_updated(self, host_id, relayout=True):
        tile = self._tiles.get(host_id)
        if tile is None:
            return
        status = self._hub.status(host_id)
        if status is None:
            tile.set_status([], None, reachable=host_id == LOCAL_ID, message="waiting for first poll…")
        else:
            tile.set_status(status.rows, status.gpu, reachable=status.reachable,
                            status_text=status_text_for(status),
                            message="" if status.reachable else status.error,
                            ollama_online=status.ollama_online)
        self._update_detail(host_id)
        self._update_summary()

    def _update_detail(self, host_id):
        tile = self._tiles.get(host_id)
        status = self._hub.status(host_id)
        if tile is None:
            return
        if host_id == LOCAL_ID:
            address = self._settings.data.get("ollama_url", "")
        else:
            host = self._settings.host(host_id)
            address = normalize_url(host.address, host.mode) if host else ""
        lines = [address or "(no address)"]
        if status is not None:
            facts = []
            if status.platform:
                facts.append(status.platform)
            if status.ollama_version:
                facts.append(f"Ollama {status.ollama_version}")
            if status.mode == MODE_OLLAMA and host_id != LOCAL_ID:
                facts.append("direct API")
            elif status.agent_version:
                facts.append(f"agent {status.agent_version}")
            if facts:
                lines.append(" · ".join(facts))
            if status.gpu.available:
                lines.append(status.gpu.name)
            if host_id != LOCAL_ID:
                lines.append(f"last seen {_ago(status.last_seen)}")
        tile.set_detail("\n".join(lines))

    def _update_summary(self):
        statuses = self._hub.all()
        remotes = [h.id for h in self._settings.hosts() if h.enabled]
        up = sum(1 for hid in remotes if statuses.get(hid) and statuses[hid].reachable)
        rows = self._hub.all_rows()
        active = sum(1 for s in rows if s.state is ModelState.ACTIVE)
        warm = sum(1 for s in rows if s.state is ModelState.WAITING)
        self.summary.setText(
            f"<b>{up + 1}/{len(remotes) + 1}</b> hosts online &nbsp;·&nbsp; "
            f"<span style='color:#4ADE80'><b>{active}</b> generating</span> &nbsp;·&nbsp; "
            f"<span style='color:#FBBF24'><b>{warm}</b> warm</span>"
        )

    def _tick(self):
        if not self.isVisible():
            return
        for host_id, tile in self._tiles.items():
            tile.tick()
            self._update_detail(host_id)


# --------------------------------------------------------------------------- #
# Hosts
# --------------------------------------------------------------------------- #

class DiscoverDialog(QDialog):
    def __init__(self, existing_addresses: set, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Discover hosts on this network")
        self.resize(520, 380)
        self._existing = existing_addresses
        layout = QVBoxLayout(self)
        self.status = QLabel("Listening for agents and sweeping the local /24 for Ollama…")
        layout.addWidget(self.status)
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        layout.addWidget(hint(
            "Agents announce themselves; bare Ollama servers are found only if they listen on "
            "the network (OLLAMA_HOST=0.0.0.0). Hosts already in your list are not shown."
        ))
        buttons = QDialogButtonBox()
        self.add_btn = buttons.addButton("Add selected", QDialogButtonBox.AcceptRole)
        self.add_btn.setEnabled(False)
        buttons.addButton(QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._relay = run_in_thread(lambda: discover(6.0), self._on_done)

    def _on_done(self, result):
        if isinstance(result, Exception):
            self.status.setText(f"Discovery failed: {result}")
            return
        fresh = [r for r in result if r["address"] not in self._existing]
        self.status.setText(f"Found {len(fresh)} new host(s)." if fresh else
                            "Nothing new found. You can still add hosts by address.")
        for entry in fresh:
            label = f"{entry['name']}  —  {entry['address']}  ({entry['via']})"
            if entry.get("token_required"):
                label += "  🔒 token required"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, entry)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.list.addItem(item)
        self.add_btn.setEnabled(bool(fresh))

    def selected(self) -> list:
        out = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.checkState() == Qt.Checked:
                out.append(item.data(Qt.UserRole))
        return out


class HostsPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self._window = window
        self._loading = False
        self._relay = None

        layout = QHBoxLayout(self)
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.setMinimumWidth(220)
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.model().rowsMoved.connect(self._on_rows_moved)
        self.list.currentRowChanged.connect(self._on_select)
        left.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(lambda: self._add_host())
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_host)
        discover_btn = QPushButton("Discover…")
        discover_btn.clicked.connect(self._discover)
        for b in (add, remove, discover_btn):
            buttons.addWidget(b)
        left.addLayout(buttons)
        left.addWidget(hint("Drag to reorder — the HUD stacks cards in this order."))
        layout.addLayout(left)

        self.form_box = QGroupBox("Host")
        form = QFormLayout(self.form_box)
        form.setLabelAlignment(Qt.AlignRight)
        self.name = QLineEdit()
        self.address = QLineEdit()
        self.address.setPlaceholderText("192.168.1.50  or  gpu-box.lan:11480")
        self.mode = QComboBox()
        self.mode.addItem("Agent — full detail (agent.py or Sharing on that machine)", MODE_AGENT)
        self.mode.addItem("Ollama API — direct, no install needed (port 11434)", MODE_OLLAMA)
        self.kind = QComboBox()
        self.kind.addItem("Computer", KIND_COMPUTER)
        self.kind.addItem("Server", KIND_SERVER)
        self.colour = ColorButton()
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("comma separated, e.g. gpu, prod")
        self.tag_preview_holder = QVBoxLayout()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("only if the agent requires one")
        self.pinned = QLineEdit()
        self.pinned.setPlaceholderText("models to always show, e.g. qwen3:8b, llama3.1:8b")
        self.enabled = QCheckBox("Monitor this host")
        self.in_overlay = QCheckBox("Show a card for it in the overlay")
        form.addRow("Name", self.name)
        form.addRow("Address", self.address)
        form.addRow("Mode", self.mode)
        form.addRow("Kind", self.kind)
        form.addRow("Border / tag colour", self.colour)
        form.addRow("Tags", self.tags)
        form.addRow("", self._wrap(self.tag_preview_holder))
        form.addRow("Token", self.token)
        form.addRow("Pinned models", self.pinned)
        form.addRow("", self.enabled)
        form.addRow("", self.in_overlay)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_result, 1)
        form.addRow("", self._wrap(test_row))
        form.addRow("", hint(
            "Agent mode: install the agent on that machine (Sharing tab has the one-liner) "
            "or turn on Sharing in its overlay. Ollama API mode: start Ollama there with "
            "OLLAMA_HOST=0.0.0.0 — you get loaded models and countdowns, but no tok/s or GPU."
        ))
        layout.addWidget(self.form_box, 1)

        for widget in (self.name, self.address, self.tags, self.token, self.pinned):
            widget.textChanged.connect(self._store_current)
        for combo in (self.mode, self.kind):
            combo.currentIndexChanged.connect(self._store_current)
        for box in (self.enabled, self.in_overlay):
            box.toggled.connect(self._store_current)
        self.colour.colorChanged.connect(self._store_current)

    @staticmethod
    def _wrap(inner_layout) -> QWidget:
        w = QWidget()
        inner_layout.setContentsMargins(0, 0, 0, 0)
        w.setLayout(inner_layout)
        return w

    # ---- draft plumbing ------------------------------------------------- #

    def _hosts(self) -> list:
        return self._window.draft.setdefault("hosts", [])

    def load(self):
        self.list.blockSignals(True)
        self.list.clear()
        for host in self._hosts():
            self.list.addItem(self._make_item(host))
        self.list.blockSignals(False)
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._on_select(-1)

    def _make_item(self, host: dict) -> QListWidgetItem:
        item = QListWidgetItem()
        self._render_item(item, host)
        return item

    def _render_item(self, item: QListWidgetItem, host: dict):
        h = HostConfig.from_dict(host)
        item.setText(f"{h.name}\n{h.address or '(no address)'}" + ("" if h.enabled else "  · off"))
        item.setIcon(swatch_icon(h.color))
        item.setData(Qt.UserRole, h.id)

    def _on_rows_moved(self, *_):
        by_id = {h["id"]: h for h in self._hosts()}
        order = [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]
        self._window.draft["hosts"] = [by_id[i] for i in order if i in by_id]

    def _on_select(self, row: int):
        hosts = self._hosts()
        has = 0 <= row < len(hosts)
        self.form_box.setEnabled(has)
        self.test_result.setText("")
        if not has:
            return
        h = HostConfig.from_dict(hosts[row])
        self._loading = True
        self.name.setText(h.name)
        self.address.setText(h.address)
        self.mode.setCurrentIndex(max(0, self.mode.findData(h.mode)))
        self.kind.setCurrentIndex(max(0, self.kind.findData(h.kind)))
        self.colour.set_colour(h.color)
        self.tags.setText(", ".join(h.tags))
        self.token.setText(h.token)
        self.pinned.setText(", ".join(h.pinned_models))
        self.enabled.setChecked(h.enabled)
        self.in_overlay.setChecked(h.show_in_overlay)
        self._loading = False
        self._refresh_tag_preview(h.tags)

    def _current_config(self) -> HostConfig:
        row = self.list.currentRow()
        base = self._hosts()[row] if 0 <= row < len(self._hosts()) else {}
        return HostConfig.from_dict({
            **base,
            "name": self.name.text().strip() or self.address.text().strip() or "Host",
            "address": self.address.text().strip(),
            "mode": self.mode.currentData(),
            "kind": self.kind.currentData(),
            "color": self.colour.colour(),
            "tags": split_list(self.tags.text()),
            "token": self.token.text().strip(),
            "pinned_models": split_list(self.pinned.text()),
            "enabled": self.enabled.isChecked(),
            "show_in_overlay": self.in_overlay.isChecked(),
        })

    def _store_current(self, *_):
        if self._loading:
            return
        row = self.list.currentRow()
        if not 0 <= row < len(self._hosts()):
            return
        host = self._current_config().to_dict()
        self._hosts()[row] = host
        self._render_item(self.list.item(row), host)
        self._refresh_tag_preview(host["tags"])

    def _refresh_tag_preview(self, tags):
        while self.tag_preview_holder.count():
            item = self.tag_preview_holder.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.tag_preview_holder.addWidget(tag_preview(tags, self._window.draft_tag_colour))

    # ---- actions -------------------------------------------------------- #

    def _add_host(self, preset: dict = None):
        colour = HOST_PALETTE[len(self._hosts()) % len(HOST_PALETTE)]
        host = HostConfig(color=colour, **(preset or {})).to_dict()
        self._hosts().append(host)
        self.list.addItem(self._make_item(host))
        self.list.setCurrentRow(self.list.count() - 1)
        if not preset:
            self.name.setFocus()
            self.name.selectAll()

    def _remove_host(self):
        row = self.list.currentRow()
        if not 0 <= row < len(self._hosts()):
            return
        name = self._hosts()[row].get("name", "this host")
        if QMessageBox.question(self, "Remove host", f"Stop monitoring {name}?") != QMessageBox.Yes:
            return
        del self._hosts()[row]
        self.list.takeItem(row)
        self._on_select(self.list.currentRow())

    def _discover(self):
        existing = {normalize_url(h.get("address", ""), h.get("mode", MODE_AGENT))
                    for h in self._hosts()}
        existing = {e.replace("http://", "") for e in existing if e}
        dialog = DiscoverDialog(existing, self)
        if dialog.exec() != QDialog.Accepted:
            return
        for entry in dialog.selected():
            self._add_host({
                "name": entry["name"],
                "address": entry["address"],
                "mode": entry["mode"],
                "kind": KIND_SERVER if entry["mode"] == MODE_OLLAMA else KIND_COMPUTER,
            })

    def _test(self):
        config = self._current_config()
        self.test_btn.setEnabled(False)
        self.test_result.setStyleSheet(f"color: {LABEL_GRAY};")
        self.test_result.setText(f"Contacting {normalize_url(config.address, config.mode) or '?'}…")
        self._relay = run_in_thread(lambda: poll_host(config), self._on_tested)

    def _on_tested(self, status):
        self.test_btn.setEnabled(True)
        if isinstance(status, Exception) or not status.reachable:
            error = status if isinstance(status, Exception) else status.error
            self.test_result.setStyleSheet(f"color: {DANGER};")
            self.test_result.setText(f"✗ {error}")
            return
        resident = len(status.resident_names)
        bits = [f"✓ reachable in {status.latency_ms:.0f} ms"]
        if status.ollama_version:
            bits.append(f"Ollama {status.ollama_version}")
        bits.append(f"{resident} model(s) loaded")
        if status.gpu.available:
            bits.append(status.gpu.name)
        if not status.ollama_online:
            bits.append("but Ollama isn't running there")
        self.test_result.setStyleSheet(f"color: {ACCENT};")
        self.test_result.setText(" · ".join(bits))


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #

class TagsPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self._window = window
        layout = QVBoxLayout(self)
        layout.addWidget(hint(
            "Tags label hosts on their cards (e.g. gpu, prod, lab). Each tag keeps its colour "
            "everywhere it appears. Tags you type on a host without defining them here get an "
            "automatic colour."
        ))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Tag", "Colour", "Preview"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        add = QPushButton("Add tag")
        add.clicked.connect(self._add)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)

    def load(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for name, colour in (self._window.draft.get("tags") or {}).items():
            self._append(name, colour)
        self.table.blockSignals(False)

    def _append(self, name: str, colour: str):
        from PySide6.QtWidgets import QTableWidgetItem
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(name))
        button = ColorButton(valid_colour(colour, auto_tag_colour(name)))
        button.colorChanged.connect(lambda _c: self.store())
        self.table.setCellWidget(r, 1, button)
        self._render_preview(r)

    def _render_preview(self, r: int):
        name_item = self.table.item(r, 0)
        button = self.table.cellWidget(r, 1)
        if name_item is None or button is None:
            return
        pill = QLabel(name_item.text() or "tag")
        pill.setStyleSheet(pill_style(button.colour()))
        holder = QWidget()
        h = QHBoxLayout(holder)
        h.setContentsMargins(6, 2, 6, 2)
        h.addWidget(pill)
        h.addStretch()
        self.table.setCellWidget(r, 2, holder)

    def _on_item_changed(self, _item):
        self.store()

    def _add(self):
        existing = {self.table.item(r, 0).text() for r in range(self.table.rowCount())
                    if self.table.item(r, 0)}
        n = 1
        while f"tag{n}" in existing:
            n += 1
        self.table.blockSignals(True)
        self._append(f"tag{n}", HOST_PALETTE[(self.table.rowCount()) % len(HOST_PALETTE)])
        self.table.blockSignals(False)
        self.store()
        self.table.editItem(self.table.item(self.table.rowCount() - 1, 0))

    def _remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self.store()

    def store(self):
        tags = {}
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            button = self.table.cellWidget(r, 1)
            if item is None or button is None or not item.text().strip():
                continue
            tags[item.text().strip()] = button.colour()
            self._render_preview(r)
        self._window.draft["tags"] = tags


# --------------------------------------------------------------------------- #
# Appearance
# --------------------------------------------------------------------------- #

class AppearancePage(QWidget):
    def __init__(self, window):
        super().__init__()
        self._window = window
        layout = QHBoxLayout(self)

        colours = QGroupBox("Colours")
        cform = QFormLayout(colours)
        self.c_active = ColorButton()
        self.c_waiting = ColorButton()
        self.c_stopped = ColorButton()
        self.c_local = ColorButton()
        self.c_computer = ColorButton()
        self.c_server = ColorButton()
        cform.addRow("ACTIVE (generating)", self.c_active)
        cform.addRow("WAITING (warm)", self.c_waiting)
        cform.addRow("STOPPED", self.c_stopped)
        cform.addRow(hint("Kind badges on each card:"))
        cform.addRow("LOCAL badge", self.c_local)
        cform.addRow("PC badge", self.c_computer)
        cform.addRow("SERVER badge", self.c_server)
        cform.addRow(hint("Each host's border and name-tag colour is set on the Hosts tab "
                          "(this machine's on General)."))
        layout.addWidget(colours, 1)

        cards = QGroupBox("Overlay")
        form = QFormLayout(cards)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(220, 480)
        self.width_spin.setSuffix(" px")
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(40, 255)
        self.border = QSpinBox()
        self.border.setRange(0, 6)
        self.border.setSuffix(" px")
        self.radius = QSpinBox()
        self.radius.setRange(0, 24)
        self.radius.setSuffix(" px")
        self.max_rows = QSpinBox()
        self.max_rows.setRange(1, 12)
        self.corner = QComboBox()
        for label, value in (("Top right", "top-right"), ("Top left", "top-left"),
                             ("Bottom right", "bottom-right"), ("Bottom left", "bottom-left")):
            self.corner.addItem(label, value)
        self.layout_combo = QComboBox()
        self.layout_combo.addItem("Stack cards vertically", "vertical")
        self.layout_combo.addItem("Cards side by side", "horizontal")
        self.show_gpu = QCheckBox("Show GPU / VRAM bars")
        form.addRow("Card width", self.width_spin)
        form.addRow("Background opacity", self.opacity)
        form.addRow("Border width", self.border)
        form.addRow("Corner radius", self.radius)
        form.addRow("Max model rows per card", self.max_rows)
        form.addRow("Screen corner", self.corner)
        form.addRow("Layout", self.layout_combo)
        form.addRow("", self.show_gpu)
        form.addRow(hint("Drag the overlay to move it; double-click it to snap back to the corner."))
        layout.addWidget(cards, 1)

    def load(self):
        a = self._window.draft["appearance"]
        self.c_active.set_colour(a["state_active"])
        self.c_waiting.set_colour(a["state_waiting"])
        self.c_stopped.set_colour(a["state_stopped"])
        self.c_local.set_colour(a["kind_local"])
        self.c_computer.set_colour(a["kind_computer"])
        self.c_server.set_colour(a["kind_server"])
        self.width_spin.setValue(int(a["width"]))
        self.opacity.setValue(int(a["opacity"]))
        self.border.setValue(int(a["border_width"]))
        self.radius.setValue(int(a["border_radius"]))
        self.max_rows.setValue(int(a["max_rows"]))
        self.corner.setCurrentIndex(max(0, self.corner.findData(a["corner"])))
        self.layout_combo.setCurrentIndex(max(0, self.layout_combo.findData(a["layout"])))
        self.show_gpu.setChecked(bool(a["show_gpu"]))

    def store(self):
        self._window.draft["appearance"].update({
            "state_active": self.c_active.colour(),
            "state_waiting": self.c_waiting.colour(),
            "state_stopped": self.c_stopped.colour(),
            "kind_local": self.c_local.colour(),
            "kind_computer": self.c_computer.colour(),
            "kind_server": self.c_server.colour(),
            "width": self.width_spin.value(),
            "opacity": self.opacity.value(),
            "border_width": self.border.value(),
            "border_radius": self.radius.value(),
            "max_rows": self.max_rows.value(),
            "corner": self.corner.currentData(),
            "layout": self.layout_combo.currentData(),
            "show_gpu": self.show_gpu.isChecked(),
        })


# --------------------------------------------------------------------------- #
# General
# --------------------------------------------------------------------------- #

class GeneralPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self._window = window
        outer = QHBoxLayout(self)
        left = QVBoxLayout()
        right = QVBoxLayout()
        outer.addLayout(left, 1)
        outer.addLayout(right, 1)

        me = QGroupBox("This machine's card")
        f = QFormLayout(me)
        self.local_name = QLineEdit()
        self.local_name.setPlaceholderText(platform_support.hostname())
        self.local_colour = ColorButton()
        self.local_tags = QLineEdit()
        self.local_tags.setPlaceholderText("comma separated")
        self.local_show = QCheckBox("Show this machine in the overlay")
        f.addRow("Display name", self.local_name)
        f.addRow("Border / tag colour", self.local_colour)
        f.addRow("Tags", self.local_tags)
        f.addRow("", self.local_show)
        left.addWidget(me)

        ollama = QGroupBox("Local Ollama")
        f = QFormLayout(ollama)
        self.ollama_url = QLineEdit()
        self.poll_ms = QSpinBox()
        self.poll_ms.setRange(500, 10000)
        self.poll_ms.setSingleStep(250)
        self.poll_ms.setSuffix(" ms")
        self.pinned = QLineEdit()
        self.pinned.setPlaceholderText("e.g. qwen3:8b, gemma4:12b")
        f.addRow("API URL", self.ollama_url)
        f.addRow("", hint("URL changes take effect after restarting the overlay."))
        f.addRow("Poll interval (idle)", self.poll_ms)
        f.addRow("Pinned models", self.pinned)
        f.addRow("", hint("Pinned models always keep a row, showing STOPPED until loaded. "
                          "OLLAMA_OVERLAY_MODELS is still honoured too."))
        left.addWidget(ollama)
        left.addStretch()

        remote = QGroupBox("Remote hosts and overlay")
        f = QFormLayout(remote)
        self.remote_poll = QSpinBox()
        self.remote_poll.setRange(1, 120)
        self.remote_poll.setSuffix(" s")
        self.auto_hide = QCheckBox("Hide the overlay when nothing is loaded and no remote hosts are shown")
        f.addRow("Remote poll interval", self.remote_poll)
        f.addRow("", self.auto_hide)
        right.addWidget(remote)

        notes = QGroupBox("Notifications")
        f = QFormLayout(notes)
        self.toasts = QCheckBox("Show toasts")
        self.offline_toasts = QCheckBox("When a remote host goes offline / comes back")
        self.remote_model_toasts = QCheckBox("When a remote host loads / unloads a model")
        self.toast_secs = QSpinBox()
        self.toast_secs.setRange(1, 30)
        self.toast_secs.setSuffix(" s")
        f.addRow("", self.toasts)
        f.addRow("", self.offline_toasts)
        f.addRow("", self.remote_model_toasts)
        f.addRow("Toast duration", self.toast_secs)
        right.addWidget(notes)

        startup = QGroupBox("Startup and files")
        f = QFormLayout(startup)
        self.autostart = QCheckBox("Start the overlay when I log in")
        self.autostart.setChecked(platform_support.autostart_enabled("overlay"))
        self.autostart.toggled.connect(self._toggle_autostart)
        self.autostart_result = hint("")
        f.addRow("", self.autostart)
        f.addRow("", self.autostart_result)
        files = QHBoxLayout()
        open_cfg = QPushButton("Open config folder")
        open_cfg.clicked.connect(lambda: platform_support.open_path(platform_support.config_dir()))
        open_log = QPushButton("Open error log")
        open_log.clicked.connect(lambda: platform_support.open_path(_LOG_PATH))
        files.addWidget(open_cfg)
        files.addWidget(open_log)
        files.addStretch()
        f.addRow(HostsPage._wrap(files))
        f.addRow(hint(f"{APP_NAME} {APP_VERSION}"))
        right.addWidget(startup)
        right.addStretch()

    def _toggle_autostart(self, on: bool):
        ok, message = platform_support.set_autostart(on, "overlay")
        self.autostart_result.setText(message)
        if not ok:
            self.autostart.blockSignals(True)
            self.autostart.setChecked(not on)
            self.autostart.blockSignals(False)

    def load(self):
        d = self._window.draft
        local = d["local"]
        self.local_name.setText(local.get("name", ""))
        self.local_colour.set_colour(local.get("color", ACCENT))
        self.local_tags.setText(", ".join(local.get("tags") or []))
        self.local_show.setChecked(bool(local.get("show", True)))
        self.ollama_url.setText(d.get("ollama_url", ""))
        self.poll_ms.setValue(int(d.get("poll_interval_ms", 1500)))
        self.pinned.setText(", ".join(d.get("pinned_models") or []))
        self.remote_poll.setValue(int(d.get("remote_poll_interval_s", 3)))
        self.auto_hide.setChecked(bool(d["behavior"].get("auto_hide_when_idle", True)))
        n = d["notifications"]
        self.toasts.setChecked(bool(n.get("toasts", True)))
        self.offline_toasts.setChecked(bool(n.get("host_offline_toasts", True)))
        self.remote_model_toasts.setChecked(bool(n.get("remote_model_toasts", False)))
        self.toast_secs.setValue(int(n.get("toast_seconds", 4)))

    def store(self):
        d = self._window.draft
        d["local"].update({
            "name": self.local_name.text().strip(),
            "color": self.local_colour.colour(),
            "tags": split_list(self.local_tags.text()),
            "show": self.local_show.isChecked(),
        })
        d["ollama_url"] = self.ollama_url.text().strip().rstrip("/") or "http://127.0.0.1:11434"
        d["poll_interval_ms"] = self.poll_ms.value()
        d["pinned_models"] = split_list(self.pinned.text())
        d["remote_poll_interval_s"] = self.remote_poll.value()
        d["behavior"]["auto_hide_when_idle"] = self.auto_hide.isChecked()
        d["notifications"].update({
            "toasts": self.toasts.isChecked(),
            "host_offline_toasts": self.offline_toasts.isChecked(),
            "remote_model_toasts": self.remote_model_toasts.isChecked(),
            "toast_seconds": self.toast_secs.value(),
        })


# --------------------------------------------------------------------------- #
# Sharing
# --------------------------------------------------------------------------- #

class SharingPage(QWidget):
    def __init__(self, window, sharing):
        super().__init__()
        self._window = window
        self._sharing = sharing
        layout = QVBoxLayout(self)

        box = QGroupBox("Share this machine's Ollama status (passthrough)")
        f = QFormLayout(box)
        self.enabled = QCheckBox("Let overlays on other computers monitor this machine")
        self.bind = QLineEdit()
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        token_row = QHBoxLayout()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("blank = no token (LAN only!)")
        show = QPushButton("Show")
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.token.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        gen = QPushButton("Generate")
        gen.clicked.connect(lambda: self.token.setText(generate_token()))
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.token.text()))
        for w in (self.token, show, gen, copy_btn):
            token_row.addWidget(w)
        self.beacon = QCheckBox("Announce on the LAN so other overlays can Discover it")
        f.addRow("", self.enabled)
        f.addRow("Listen address", self.bind)
        f.addRow("Port", self.port)
        f.addRow("Token", HostsPage._wrap(token_row))
        f.addRow("", self.beacon)
        self.state = QLabel("")
        self.state.setWordWrap(True)
        self.state.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow("Status", self.state)
        layout.addWidget(box)

        how = QGroupBox("Monitoring servers without a desktop")
        v = QVBoxLayout(how)
        v.addWidget(hint(
            "On a headless Linux server, install just the agent (it runs as a systemd service, "
            "no GUI needed) and add it on the Hosts tab with the token it prints:"
        ))
        linux = QLineEdit(f"curl -fsSL {REPO_RAW}/install.sh | bash -s -- --agent")
        windows = QLineEdit(f"& ([scriptblock]::Create((irm {REPO_RAW}/install.ps1))) -Agent")
        for line in (linux, windows):
            line.setReadOnly(True)
            line.setCursorPosition(0)
        v.addWidget(QLabel("Linux"))
        v.addWidget(linux)
        v.addWidget(QLabel("Windows (PowerShell)"))
        v.addWidget(windows)
        v.addWidget(hint(
            "The agent is read-only: it reports models, states, tok/s and GPU stats and cannot "
            "load, unload or stop anything. Allow the port through the firewall on that machine "
            "(the installers try to do this for you)."
        ))
        layout.addWidget(how)
        layout.addStretch()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_state)
        self._timer.start(1500)

    def load(self):
        s = self._window.draft["sharing"]
        self.enabled.setChecked(bool(s.get("enabled")))
        self.bind.setText(s.get("bind", "0.0.0.0"))
        self.port.setValue(int(s.get("port", DEFAULT_AGENT_PORT)))
        self.token.setText(s.get("token", ""))
        self.beacon.setChecked(bool(s.get("beacon", True)))
        self._refresh_state()

    def store(self):
        if self.enabled.isChecked() and not self.token.text().strip():
            # Sharing with no token is allowed, but default to having one.
            if not self._window.draft["sharing"].get("enabled"):
                self.token.setText(generate_token())
        self._window.draft["sharing"].update({
            "enabled": self.enabled.isChecked(),
            "bind": self.bind.text().strip() or "0.0.0.0",
            "port": self.port.value(),
            "token": self.token.text().strip(),
            "beacon": self.beacon.isChecked(),
        })

    def _refresh_state(self):
        server = self._sharing.server
        if server.running:
            bind, port, token = server.config
            ip = local_ipv4() or "this-machine"
            self.state.setStyleSheet(f"color: {ACCENT};")
            self.state.setText(
                f"Serving on {bind}:{port}. Add this machine elsewhere as  {ip}:{port}  "
                f"(mode: Agent){' with the token above' if token else ''}."
            )
        elif server.last_error:
            self.state.setStyleSheet(f"color: {DANGER};")
            self.state.setText(server.last_error)
        else:
            self.state.setStyleSheet(f"color: {LABEL_GRAY};")
            self.state.setText("Not sharing.")


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #

class SettingsWindow(QWidget):
    def __init__(self, settings, hub, poller, sharing, controller, overlay):
        super().__init__()
        self._settings = settings
        self.setObjectName("settingsRoot")
        self.setWindowTitle(f"{APP_NAME} — Settings & Dashboard")
        self.setStyleSheet(STYLE)
        self.resize(1040, 700)
        self.draft = settings.snapshot()
        self._stale = False

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.dashboard = DashboardPage(settings, hub, poller, controller, overlay)
        self.hosts_page = HostsPage(self)
        self.tags_page = TagsPage(self)
        self.appearance_page = AppearancePage(self)
        self.general_page = GeneralPage(self)
        self.sharing_page = SharingPage(self, sharing)
        self.tabs.addTab(self.dashboard, "Dashboard")
        self.tabs.addTab(self.hosts_page, "Hosts")
        self.tabs.addTab(self.tags_page, "Tags")
        self.tabs.addTab(self.appearance_page, "Appearance")
        self.tabs.addTab(self.general_page, "General")
        self.tabs.addTab(self.sharing_page, "Sharing")
        layout.addWidget(self.tabs, 1)

        bar = QHBoxLayout()
        self.saved_label = hint("")
        bar.addWidget(self.saved_label, 1)
        revert = QPushButton("Revert")
        revert.clicked.connect(self.revert)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self.apply)
        save = QPushButton("Save && close")
        save.setObjectName("primary")
        save.clicked.connect(self.save_and_close)
        for b in (revert, apply_btn, save):
            bar.addWidget(b)
        layout.addLayout(bar)
        self._load_pages()

    def draft_tag_colour(self, tag: str) -> str:
        for name, colour in (self.draft.get("tags") or {}).items():
            if name.lower() == tag.lower():
                return valid_colour(colour, auto_tag_colour(tag))
        return auto_tag_colour(tag)

    def _load_pages(self):
        for page in (self.hosts_page, self.tags_page, self.appearance_page,
                     self.general_page, self.sharing_page):
            page.load()

    def _store_pages(self):
        for page in (self.tags_page, self.appearance_page, self.general_page, self.sharing_page):
            page.store()

    def apply(self):
        self._store_pages()
        self._settings.replace(copy.deepcopy(self.draft))
        self.saved_label.setText(f"Saved to {self._settings.path}")
        self.sharing_page.load()

    def save_and_close(self):
        self.apply()
        self.close()

    def revert(self):
        self.draft = self._settings.snapshot()
        self._load_pages()
        self.saved_label.setText("Reverted to the saved settings.")

    def hideEvent(self, event):
        self._stale = True
        super().hideEvent(event)

    def showEvent(self, event):
        # Reopening the window starts from what's saved, not a stale draft.
        if self._stale:
            self._stale = False
            self.draft = self._settings.snapshot()
            self._load_pages()
        super().showEvent(event)
