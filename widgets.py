"""
Widgets shared by the HUD and the dashboard.

- `ModelRow` — one roster model: name + state pill, VRAM, tok/s, countdown.
- `HostCard` — one machine: a card bordered in the host's colour, with a name
  tag, a kind badge (local / computer / server), the host's tags as coloured
  pills, its model rows, and its GPU/VRAM bars. The HUD stacks one per host;
  the dashboard lays them out in a grid with an extra detail line.
- `WindowControls` — the overlay's own minimise/close buttons. They live in
  the header row of whichever card is on top rather than in a separate title
  bar, so the overlay is nothing but cards.
- `ClaudeCard` — Claude Code sessions on this machine and the token volume
  through them, drawn in the same visual language: a session that is generating
  reads green like an active model, one waiting for input reads amber like a
  warm one.
"""
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

import theme
from claude_watch import ClaudeStatus, fmt_tokens
from gpu_stats import GPUStats
from model_state import ModelState
from monitor import ModelStatus
from theme import BORDER, DANGER, LABEL_GRAY, VRAM_BLUE, fmt_duration, mono, pill_style, rgba


class Bar(QWidget):
    """Tiny horizontal usage bar."""
    def __init__(self, color=theme.ACCENT, parent=None):
        super().__init__(parent)
        self.value = 0.0  # 0..1
        self.color = color
        self.setFixedHeight(5)

    def set_value(self, v):
        self.value = max(0.0, min(1.0, v))
        self.update()

    def set_color(self, color):
        self.color = color
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 25)))
        p.drawRoundedRect(0, 0, w, h, 2, 2)
        p.setBrush(QBrush(QColor(self.color)))
        p.drawRoundedRect(0, 0, int(w * self.value), h, 2, 2)


class WindowControls(QWidget):
    """The overlay's minimise and close buttons.

    One instance is moved between cards: whichever card is currently on top
    hosts it, so the controls always sit in the overlay's top-right corner
    without a title bar of their own to live in.
    """

    minimizeToggled = Signal()
    closeClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.minimize_btn = self._button("\u2013", 13, "Collapse the overlay")
        self.minimize_btn.clicked.connect(lambda *_: self.minimizeToggled.emit())
        self.close_btn = self._button("\u00d7", 15, "Hide the overlay (the tray icon brings it back)")
        self.close_btn.clicked.connect(lambda *_: self.closeClicked.emit())
        row.addWidget(self.minimize_btn)
        row.addWidget(self.close_btn)

    @staticmethod
    def _button(glyph, size, tooltip):
        btn = QPushButton(glyph)
        btn.setFixedSize(14, 14)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(tooltip)
        btn.setStyleSheet(
            "QPushButton { background: transparent; color: %s; border: none;"
            " font-size: %dpx; font-weight: bold; padding: 0; }"
            " QPushButton:hover { color: white; }" % (LABEL_GRAY, size)
        )
        return btn

    def set_minimized(self, minimized: bool):
        self.minimize_btn.setText("+" if minimized else "\u2013")
        self.minimize_btn.setToolTip("Expand the overlay" if minimized
                                     else "Collapse the overlay")


class ModelRow(QWidget):
    """One compact row per roster model: name + state pill, then VRAM, tok/s,
    and the keep_alive countdown."""

    def __init__(self, name: str, name_width: int = 176, host_label: str = "", parent=None):
        super().__init__(parent)
        self.name = name
        self._name_width = name_width
        self._host_label = host_label
        self._state = ModelState.STOPPED
        self._seconds_left = None
        # Wall-clock stamp of the last status update, so the countdown can be
        # interpolated every second instead of jumping in poll-sized steps.
        self._stamped_at = time.monotonic()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 6)
        layout.setSpacing(2)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(6)
        self.name_label = QLabel()
        self.name_label.setStyleSheet("color: white;")
        self.name_label.setFont(mono(11, QFont.DemiBold))
        self.name_label.setToolTip(name)
        self._set_elided_name(name)
        self.state_pill = QLabel("")
        self.state_pill.setFont(mono(7, QFont.DemiBold))
        self.state_pill.setAlignment(Qt.AlignCenter)
        top_row.addWidget(self.name_label)
        top_row.addStretch()
        top_row.addWidget(self.state_pill)
        layout.addLayout(top_row)

        sub_row = QHBoxLayout()
        sub_row.setContentsMargins(0, 0, 0, 0)
        self.vram_label = QLabel("~0.0G")
        self.vram_label.setStyleSheet(f"color: {LABEL_GRAY};")
        self.vram_label.setFont(mono(8))
        self.vram_label.setToolTip("Ollama-reported VRAM footprint (approximate)")
        self.speed_label = QLabel("—")
        self.speed_label.setStyleSheet(f"color: {theme.ACCENT};")
        self.speed_label.setFont(mono(8, QFont.DemiBold))
        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet(f"color: {LABEL_GRAY};")
        self.meta_label.setFont(mono(8))
        self.meta_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        sub_row.addWidget(self.vram_label)
        sub_row.addSpacing(8)
        sub_row.addWidget(self.speed_label)
        sub_row.addStretch()
        sub_row.addWidget(self.meta_label)
        layout.addLayout(sub_row)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {BORDER};")
        layout.addWidget(divider)

    def _set_elided_name(self, name: str):
        metrics = QFontMetrics(self.name_label.font())
        self.name_label.setText(metrics.elidedText(name, Qt.ElideRight, self._name_width))

    def set_status(self, status: ModelStatus):
        self._state = status.state
        self._seconds_left = status.seconds_left
        self._stamped_at = time.monotonic()

        fg, pill_bg = theme.STATE_STYLE.get(status.state, theme.STATE_STYLE[ModelState.STOPPED])
        self.state_pill.setText(status.state.value)
        self.state_pill.setStyleSheet(
            f"color: {fg}; background: {pill_bg}; border-radius: 6px; padding: 1px 5px;"
        )
        self.name_label.setStyleSheet(
            "color: white;" if status.is_resident else f"color: {LABEL_GRAY};"
        )

        if status.is_resident:
            self.vram_label.setText(f"~{status.size_vram_gb:.1f}G")
        else:
            self.vram_label.setText("—")

        active_colour = theme.state_colour(ModelState.ACTIVE)
        if status.tok_s:
            self.speed_label.setText(f"{status.tok_s:.1f} tok/s")
            # Dim the last measured speed once the model stops generating, so a
            # stale figure can't be misread as current throughput.
            colour = active_colour if status.state is ModelState.ACTIVE else theme.dim(active_colour)
            self.speed_label.setStyleSheet(f"color: {colour};")
        else:
            self.speed_label.setText("—")
            self.speed_label.setStyleSheet(f"color: {LABEL_GRAY};")

        self.setToolTip(self._tooltip(status))
        self.refresh_countdown()

    def _tooltip(self, status: ModelStatus) -> str:
        how = {
            "slots": "runner /slots is_processing",
            "nvml": "per-process GPU utilization",
            "expiry": "keep_alive deadline moved (request just finished)",
        }.get(status.source, "no live signal — resident/not-resident only")
        lines = [status.name]
        if self._host_label:
            lines.append(f"on {self._host_label}")
        lines.append(f"state: {status.state.value} (via {how})")
        if status.seconds_left is not None:
            lines.append(f"unloads in {fmt_duration(status.seconds_left)}")
        if status.pinned:
            lines.append("pinned")
        return "\n".join(lines)

    def refresh_countdown(self):
        """Re-render the trailing meta text; safe to call every second."""
        if self._state is ModelState.STOPPED:
            self.meta_label.setText("not loaded")
            return
        if self._state is ModelState.ACTIVE:
            self.meta_label.setText("generating")
            return
        if self._seconds_left is None:
            self.meta_label.setText("no expiry")
            return
        remaining = self._seconds_left - (time.monotonic() - self._stamped_at)
        self.meta_label.setText(f"{fmt_duration(remaining)} left")


def _small_pill(text: str, colour: str, size: int = 7) -> QLabel:
    pill = QLabel(text)
    pill.setFont(mono(size, QFont.DemiBold))
    pill.setAlignment(Qt.AlignCenter)
    pill.setStyleSheet(pill_style(colour))
    pill.setFixedHeight(pill.sizeHint().height())
    return pill


def status_text_for(status) -> str:
    """Short right-aligned header text for a card: model counts + latency."""
    if status is None:
        return "…"
    active = sum(1 for s in status.rows if s.state is ModelState.ACTIVE)
    waiting = sum(1 for s in status.rows if s.state is ModelState.WAITING)
    parts = []
    if active:
        parts.append(f"{active} active")
    elif waiting:
        parts.append(f"{waiting} warm")
    if status.latency_ms is not None:
        parts.append(f"{status.latency_ms:.0f}ms")
    return " · ".join(parts)


KIND_LABELS = {"local": "LOCAL", "computer": "PC", "server": "SERVER"}


class HostCard(QFrame):
    """One machine's card: coloured border + name tag + tags + model rows + GPU."""

    collapseToggled = Signal(str, bool)

    PAD_X = 12

    def __init__(self, host_id: str, width: int = 272, parent=None):
        super().__init__(parent)
        self.host_id = host_id
        self.setObjectName("hostcard")
        self._width = width
        self._max_rows = 6
        self._show_gpu = True
        self._collapsed = False
        self._colour = theme.ACCENT
        self._border = (1, 14, 175)
        self._rows: dict = {}
        self._row_order: list = []
        self._last_gpu = GPUStats(available=False)
        self._reachable = True
        self._name = host_id
        self._tags: list = []
        self._last_args = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(self.PAD_X, 10, self.PAD_X, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(5)
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color: {theme.IDLE_GREY}; font-size: 10px;")
        self.name_pill = QLabel()
        self.name_pill.setFont(mono(8, QFont.Bold))
        self.kind_pill = QLabel()
        self.kind_pill.setFont(mono(6, QFont.DemiBold))
        self.status_label = QLabel("")
        self.status_label.setFont(mono(7))
        self.status_label.setStyleSheet(f"color: {LABEL_GRAY};")
        self.collapse_btn = QPushButton("▾")
        self.collapse_btn.setFixedSize(14, 14)
        self.collapse_btn.setCursor(Qt.PointingHandCursor)
        self.collapse_btn.setToolTip("Collapse / expand this host")
        self.collapse_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {LABEL_GRAY}; border: none;"
            f" font-size: 10px; padding: 0; }} QPushButton:hover {{ color: white; }}"
        )
        self.collapse_btn.clicked.connect(self._toggle_collapsed)
        header.addWidget(self.dot)
        header.addWidget(self.name_pill)
        header.addWidget(self.kind_pill)
        header.addStretch()
        header.addWidget(self.status_label)
        header.addWidget(self.collapse_btn)
        layout.addLayout(header)
        self._header_row = header

        self.tag_container = QWidget()
        self.tag_row = QHBoxLayout(self.tag_container)
        self.tag_row.setContentsMargins(0, 0, 0, 0)
        self.tag_row.setSpacing(4)
        self.tag_container.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout.addWidget(self.tag_container)

        self.message_label = QLabel("")
        self.message_label.setFont(mono(8))
        self.message_label.setWordWrap(True)
        self.message_label.setStyleSheet(f"color: {LABEL_GRAY};")
        layout.addWidget(self.message_label)

        self.model_stack = QVBoxLayout()
        self.model_stack.setSpacing(2)
        layout.addLayout(self.model_stack)

        self.gpu_container, self.gpu_bar, self.gpu_value = self._bar_row("GPU", theme.ACCENT, 36)
        layout.addWidget(self.gpu_container)
        self.vram_container, self.vram_bar, self.vram_value = self._bar_row("VRAM", VRAM_BLUE, 70)
        layout.addWidget(self.vram_container)

        self.detail_label = QLabel("")
        self.detail_label.setFont(mono(7))
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet(f"color: {LABEL_GRAY};")
        self.detail_label.setVisible(False)
        layout.addWidget(self.detail_label)

        self._apply_frame_style()
        self.setFixedWidth(width)

    # ---- construction helpers ------------------------------------------- #

    def _bar_row(self, label_text, colour, value_width):
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        label = QLabel(label_text)
        label.setStyleSheet(f"color: {LABEL_GRAY};")
        label.setFont(mono(9))
        label.setFixedWidth(36)
        value = QLabel("0%")
        value.setStyleSheet("color: white;")
        value.setFont(mono(9))
        value.setFixedWidth(value_width)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bar = Bar(colour)
        row.addWidget(label)
        row.addWidget(bar, 1)
        row.addWidget(value)
        return container, bar, value

    def set_window_controls(self, controls):
        """Host the overlay's minimise/close buttons in this card's header."""
        if controls is not None:
            self._header_row.addWidget(controls)
            controls.setVisible(True)

    def _apply_frame_style(self):
        width, radius, alpha = self._border
        self.setStyleSheet(f"""
            #hostcard {{
                background: rgba(18, 18, 20, {alpha});
                border: {width}px solid {rgba(self._colour, 190)};
                border-radius: {radius}px;
            }}
        """)

    # ---- configuration --------------------------------------------------- #

    def set_identity(self, name: str, colour: str, kind: str, kind_colour: str, tags: list):
        self._colour = colour
        self._name = name
        metrics = QFontMetrics(self.name_pill.font())
        self.name_pill.setText(metrics.elidedText(name, Qt.ElideRight, self._width - 130))
        self.name_pill.setToolTip(name)
        self.name_pill.setStyleSheet(
            f"color: #0f0f10; background: {colour}; border-radius: 6px; padding: 1px 6px;"
        )
        kind_text = KIND_LABELS.get(kind, kind.upper())
        self.kind_pill.setText(kind_text)
        self.kind_pill.setVisible(bool(kind_text))
        self.kind_pill.setStyleSheet(pill_style(kind_colour, radius=5))
        self._tags = list(tags)
        self._rebuild_tags()
        self._apply_frame_style()

    def set_appearance(self, width: int, border_width: int, radius: int, alpha: int,
                       max_rows: int, show_gpu: bool):
        width_changed = width != self._width
        self._width = width
        self._border = (max(0, border_width), max(0, radius), max(0, min(255, alpha)))
        self._max_rows = max(1, max_rows)
        self._show_gpu = show_gpu
        self.setFixedWidth(width)
        self._apply_frame_style()
        if width_changed:
            # Rows elide their names to the old width; rebuild them at the new one.
            for row in self._rows.values():
                self.model_stack.removeWidget(row)
                row.deleteLater()
            self._rows.clear()
            self._row_order = []
            self._rebuild_tags()
        if self._last_args is not None:
            self.set_status(*self._last_args)

    def _rebuild_tags(self):
        while self.tag_row.count():
            item = self.tag_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        available = self._width - 2 * self.PAD_X - 4
        used = 0
        shown = 0
        for text, colour in self._tags:
            pill = _small_pill(text, colour)
            needed = pill.sizeHint().width() + 4
            remaining = len(self._tags) - shown
            reserve = 34 if remaining > 1 else 0
            if used + needed + reserve > available:
                break
            self.tag_row.addWidget(pill)
            used += needed
            shown += 1
        if shown < len(self._tags):
            more = _small_pill(f"+{len(self._tags) - shown}", LABEL_GRAY)
            more.setToolTip(", ".join(t for t, _ in self._tags[shown:]))
            self.tag_row.addWidget(more)
        self.tag_row.addStretch()
        self.tag_container.setVisible(bool(self._tags) and not self._collapsed)

    def set_detail(self, text: str):
        self.detail_label.setText(text)
        self.detail_label.setVisible(bool(text) and not self._collapsed)

    # ---- collapse --------------------------------------------------------- #

    def _toggle_collapsed(self):
        self.set_collapsed(not self._collapsed)
        self.collapseToggled.emit(self.host_id, self._collapsed)

    def set_collapsed(self, collapsed: bool):
        self._collapsed = collapsed
        self.collapse_btn.setText("▸" if collapsed else "▾")
        self._apply_visibility()

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    # ---- live data -------------------------------------------------------- #

    @property
    def has_rows(self) -> bool:
        return bool(self._rows)

    def set_status(self, rows: list, gpu, reachable: bool = True,
                   status_text: str = "", message: str = "", ollama_online: bool = True):
        self._last_args = (rows, gpu, reachable, status_text, message, ollama_online)
        self._reachable = reachable
        self._last_gpu = gpu or GPUStats(available=False)

        shown = rows[: self._max_rows] if reachable else []
        shown_names = [s.name for s in shown]

        for name in list(self._rows):
            if name not in shown_names:
                row = self._rows.pop(name)
                self.model_stack.removeWidget(row)
                row.deleteLater()

        name_width = self._width - 2 * self.PAD_X - 72
        for i, status in enumerate(shown):
            row = self._rows.get(status.name)
            if row is None:
                row = ModelRow(status.name, name_width=name_width, host_label=self._name)
                self._rows[status.name] = row
                self.model_stack.insertWidget(i, row)
            row.set_status(status)

        # Rows arrive ordered active-first; only touch the layout when that
        # order actually changed, so a steady state never reshuffles.
        if shown_names != self._row_order:
            for i, name in enumerate(shown_names):
                self.model_stack.insertWidget(i, self._rows[name])
            self._row_order = shown_names

        gpu = self._last_gpu
        if gpu.available:
            self.gpu_value.setText(f"{gpu.gpu_pct:.0f}%")
            self.gpu_bar.set_value(gpu.gpu_pct / 100.0)
            self.gpu_bar.set_color(theme.state_colour(ModelState.ACTIVE))
            self.vram_value.setText(f"{gpu.vram_used_gb:.1f}/{gpu.vram_total_gb:.1f}G")
            self.vram_bar.set_value(gpu.vram_used_gb / max(gpu.vram_total_gb, 0.001))
            self.gpu_container.setToolTip(gpu.name)
            self.vram_container.setToolTip(gpu.name)

        if not reachable:
            dot = DANGER
            self.status_label.setText("offline")
            self.status_label.setStyleSheet(f"color: {DANGER};")
            self.message_label.setText(message or "unreachable")
            self.message_label.setStyleSheet(f"color: {DANGER};")
        else:
            dot = theme.aggregate_colour(shown)
            if not ollama_online:
                self.status_label.setText("ollama down")
                self.status_label.setStyleSheet(f"color: {DANGER};")
            else:
                self.status_label.setText(status_text)
                self.status_label.setStyleSheet(f"color: {LABEL_GRAY};")
            if not shown:
                self.message_label.setText(message or "no models loaded")
            else:
                self.message_label.setText("")
            self.message_label.setStyleSheet(f"color: {LABEL_GRAY};")
        self.dot.setStyleSheet(f"color: {dot}; font-size: 10px;")
        self._apply_visibility()

    def _apply_visibility(self):
        expanded = not self._collapsed
        for row in self._rows.values():
            row.setVisible(expanded)
        self.tag_container.setVisible(expanded and bool(self._tags))
        self.message_label.setVisible(expanded and bool(self.message_label.text()))
        gpu_visible = expanded and self._show_gpu and self._reachable and self._last_gpu.available
        self.gpu_container.setVisible(gpu_visible)
        self.vram_container.setVisible(gpu_visible)
        self.detail_label.setVisible(expanded and bool(self.detail_label.text()))

    def tick(self):
        if self._collapsed:
            return
        for row in self._rows.values():
            row.refresh_countdown()


class ClaudeSessionRow(QWidget):
    """One Claude Code session: dot, name, and a BUSY/IDLE pill."""

    def __init__(self, width: int, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 1, 0, 1)
        row.setSpacing(6)
        self.dot = QLabel("●")
        self.dot.setFont(mono(6))
        self.name = QLabel("")
        self.name.setFont(mono(8))
        self.name.setStyleSheet("color: white;")
        self.pill = QLabel("")
        self.pill.setFont(mono(6, QFont.DemiBold))
        self.pill.setAlignment(Qt.AlignCenter)
        row.addWidget(self.dot)
        row.addWidget(self.name)
        row.addStretch()
        row.addWidget(self.pill)
        self._width = width

    def set_width(self, width: int):
        self._width = width

    def set_session(self, session):
        # Busy reads green and idle amber, the same way an active model row and
        # a warm one do, so the whole HUD is read with one colour vocabulary.
        state = ModelState.ACTIVE if session.busy else ModelState.WAITING
        colour = theme.state_colour(state)
        self.dot.setStyleSheet("color: %s;" % colour)
        metrics = QFontMetrics(self.name.font())
        self.name.setText(metrics.elidedText(session.label, Qt.ElideRight,
                                             max(60, self._width - 118)))
        self.name.setStyleSheet("color: white;" if session.busy
                                else "color: %s;" % LABEL_GRAY)
        self.pill.setText("BUSY" if session.busy else "IDLE")
        self.pill.setStyleSheet(pill_style(colour, radius=5))
        detail = [session.label]
        if session.cwd:
            detail.append(session.cwd)
        tail = "%s · pid %s" % (session.entrypoint or "claude", session.pid)
        if session.version:
            tail += " · v%s" % session.version
        detail.append(tail)
        self.setToolTip("\n".join(detail))


class ClaudeCard(QFrame):
    """Claude on this machine: desktop app, live sessions, token volume.

    The token figures are measured from Claude Code's own transcripts. They are
    deliberately *not* presented as a percentage of a plan limit: that number is
    only available inside Claude Code's own `/usage` view, so any percentage
    shown here would be invented.
    """

    PAD_X = 12
    USAGE_WINDOWS = (("5h", "5h"), ("7d", "7d"))

    def __init__(self, width: int = 272, parent=None):
        super().__init__(parent)
        self.setObjectName("claudecard")
        self._width = width
        self._colour = theme.ACCENT
        self._border = (1, 14, 175)
        self._max_sessions = 4
        self._rows = []
        self._collapsed = False
        self._status = ClaudeStatus()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(self.PAD_X, 10, self.PAD_X, 10)
        layout.setSpacing(5)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        self.name_pill = QLabel("CLAUDE")
        self.name_pill.setFont(mono(8, QFont.DemiBold))
        self.summary = QLabel("")
        self.summary.setFont(mono(7))
        self.summary.setStyleSheet("color: %s;" % LABEL_GRAY)
        self.summary.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addWidget(self.name_pill)
        head.addStretch()
        head.addWidget(self.summary)
        layout.addLayout(head)
        self._header_row = head

        self.sessions_box = QVBoxLayout()
        self.sessions_box.setContentsMargins(0, 0, 0, 0)
        self.sessions_box.setSpacing(1)
        layout.addLayout(self.sessions_box)

        self.more_label = QLabel("")
        self.more_label.setFont(mono(7))
        self.more_label.setStyleSheet("color: %s;" % LABEL_GRAY)
        layout.addWidget(self.more_label)

        self.message = QLabel("")
        self.message.setFont(mono(8))
        self.message.setStyleSheet("color: %s;" % LABEL_GRAY)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        self.usage_rows = {}
        for key, caption in self.USAGE_WINDOWS:
            container = QWidget()
            line = QHBoxLayout(container)
            line.setContentsMargins(0, 0, 0, 0)
            cap = QLabel(caption)
            cap.setFont(mono(8))
            cap.setStyleSheet("color: %s;" % LABEL_GRAY)
            cap.setFixedWidth(26)
            value = QLabel("—")
            value.setFont(mono(8, QFont.DemiBold))
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            line.addWidget(cap)
            line.addStretch()
            line.addWidget(value)
            layout.addWidget(container)
            self.usage_rows[key] = (container, value)

        self._apply_frame_style()
        self.setFixedWidth(width)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Maximum)

    # ---- configuration ---------------------------------------------------- #

    def set_window_controls(self, controls):
        """Host the overlay's minimise/close buttons in this card's header."""
        if controls is not None:
            self._header_row.addWidget(controls)
            controls.setVisible(True)

    def set_collapsed(self, collapsed: bool):
        """Collapse to the header row, matching HostCard's own collapse."""
        self._collapsed = bool(collapsed)
        self._apply_visibility()

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def _apply_visibility(self):
        expanded = not self._collapsed
        for index, row in enumerate(self._rows):
            row.setVisible(expanded and index < len(self._status.sessions))
        hidden = len(self._status.sessions) - min(len(self._rows), self._max_sessions)
        self.more_label.setVisible(expanded and hidden > 0)
        self.message.setVisible(expanded and bool(self.message.text()))
        for key, (container, _value) in self.usage_rows.items():
            usage = self._status.usage.get(key)
            container.setVisible(expanded and usage is not None and not usage.empty)

    def _apply_frame_style(self):
        width, radius, alpha = self._border
        self.setStyleSheet("""
            #claudecard {
                background: rgba(18, 18, 20, %d);
                border: %dpx solid %s;
                border-radius: %dpx;
            }
        """ % (alpha, width, rgba(self._colour, 190), radius))
        self.name_pill.setStyleSheet(
            "color: #0f0f10; background: %s; border-radius: 6px; padding: 1px 6px;" % self._colour
        )

    def set_appearance(self, width: int, border_width: int, radius: int, alpha: int,
                       colour: str, max_sessions: int):
        self._width = width
        self._colour = theme.valid_colour(colour, theme.ACCENT)
        self._border = (border_width, radius, alpha)
        self._max_sessions = max(1, int(max_sessions))
        self.setFixedWidth(width)
        for row in self._rows:
            row.set_width(width)
        self._apply_frame_style()
        self.set_status(self._status)

    # ---- content ---------------------------------------------------------- #

    def set_status(self, status):
        self._status = status or ClaudeStatus()
        status = self._status

        sessions = status.sessions[: self._max_sessions]
        while len(self._rows) < len(sessions):
            row = ClaudeSessionRow(self._width)
            self._rows.append(row)
            self.sessions_box.addWidget(row)
        for index, row in enumerate(self._rows):
            if index < len(sessions):
                row.set_session(sessions[index])
            row.setVisible(index < len(sessions))

        hidden = len(status.sessions) - len(sessions)
        self.more_label.setText("+%d more" % hidden if hidden > 0 else "")
        self.more_label.setVisible(hidden > 0)

        self.summary.setText(self._summary_text(status))
        self._set_message(status)
        self._set_usage(status)
        self._apply_visibility()
        self.setToolTip(self._tooltip(status))

    @staticmethod
    def _summary_text(status) -> str:
        if not status.available:
            return "not installed"
        if not status.running:
            return "not running"
        parts = []
        if status.busy_sessions:
            parts.append("%d busy" % status.busy_sessions)
        if status.sessions:
            count = len(status.sessions)
            parts.append("%d session%s" % (count, "" if count == 1 else "s"))
        elif status.app_running:
            parts.append("app open")
        return " · ".join(parts)

    def _set_message(self, status):
        text = ""
        if not status.available:
            text = "No ~/.claude on this machine."
        elif not status.running:
            text = "Claude isn't running."
        elif not status.sessions and status.app_running:
            text = "Desktop app open, no sessions."
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def _set_usage(self, status):
        for key, (container, value) in self.usage_rows.items():
            usage = status.usage.get(key)
            if usage is None or usage.empty:
                # Nothing measured in this window: hide the row rather than
                # showing a zero that reads like "you used nothing".
                container.setVisible(False)
                continue
            container.setVisible(True)
            value.setText("%s out · %s in" % (fmt_tokens(usage.output_tokens),
                                                   fmt_tokens(usage.fresh_input)))
            value.setStyleSheet("color: %s;" % theme.ACCENT)

    @staticmethod
    def _tooltip(status) -> str:
        lines = ["Claude on this machine"]
        if status.app_running:
            lines.append("desktop app running (%d helper processes)" % status.app_helpers)
        lines.append("%d live session(s), %d generating"
                     % (len(status.sessions), status.busy_sessions))
        for key in ("5h", "today", "7d"):
            usage = status.usage.get(key)
            if usage is None or usage.empty:
                continue
            lines.append("%s: %s out, %s in, %s cache-read, %d replies"
                         % (key, fmt_tokens(usage.output_tokens),
                            fmt_tokens(usage.fresh_input),
                            fmt_tokens(usage.cache_read), usage.messages))
            if key == "7d":
                models = {m: v for m, v in usage.by_model.items() if not m.startswith("<")}
                for name, counts in sorted(models.items(), key=lambda kv: -kv[1][1])[:4]:
                    lines.append("    %s: %s out" % (name, fmt_tokens(counts[1])))
        if status.usage_error:
            lines.append(status.usage_error)
        lines.append("Measured from local transcripts — not a plan-limit "
                     "percentage, which only Claude Code's /usage can show.")
        return "\n".join(lines)
