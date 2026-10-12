"""
Local AI Overlay Pro — Ollama HUD + toast, for this machine and the others you
watch. Runs on Windows and Linux from the same code.

The always-on-top HUD stacks one card per host: this machine first (driven by
OllamaMonitor, see monitor.py), then every remote computer/server configured
in Settings (driven by RemotePoller, see remote.py). Each card is bordered in
its host's colour and carries a name tag, a kind badge (LOCAL / PC / SERVER)
and the host's own coloured tags, so a glance tells you which box is which.
Every card lists that host's models with their run state (ACTIVE / WAITING /
STOPPED), eviction countdown and live tok/s, plus GPU%/VRAM where available.

A toast fires on local load/switch/unload transitions and, if enabled, on
remote hosts going offline/online or loading models.

There is no title bar: the – (collapse every card to its header row) and ×
(hide; the tray icon brings it back) buttons live in the top card's own header.
Stopping the server, unloading models, settings and quit are on the right-click
menu and in the tray menu.

To make *this* machine visible to overlays elsewhere, turn on Settings ->
Sharing (or run agent.py headless on a server); see status_server.py.

Cross-platform notes: transparency needs a compositing window manager (Windows
DWM always; on Linux GNOME/KDE/Cinnamon by default, minimal WMs need e.g.
picom). On Wayland sessions the HUD runs through XWayland so it can stay on top
and keep its corner — see platform_support.prefer_xcb_on_wayland().
"""
import argparse
import sys
import time

import platform_support
from platform_support import APP_NAME, APP_VERSION

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QPoint, Signal
from PySide6.QtGui import QGuiApplication, QFont, QColor, QIcon, QPixmap, QPainter, QBrush, QAction
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QLayout, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame,
    QMessageBox, QSystemTrayIcon, QMenu,
)

import theme
from discovery import BeaconSender
from error_log import log_exception
from gpu_stats import get_gpu_reader
from hub import StatusHub
from model_state import ModelState
from monitor import OllamaMonitor
from ollama_control import OllamaController
from remote import RemotePoller
from settings import LOCAL_ID, Settings
from status_server import StatusServer, build_snapshot
from theme import ACCENT, BORDER, DANGER, IDLE_GREY, LABEL_GRAY, mono
from claude_watch import ClaudeWatcher
from widgets import ClaudeCard, HostCard, WindowControls, status_text_for


class OverlayHUD(QWidget):
    """Always-on-top HUD: nothing but cards.

    There is no title bar. The minimise and close buttons are a single
    `WindowControls` widget that is re-parented into the header row of whichever
    card is on top, so the overlay costs no vertical space on chrome. Everything
    the old header strip also carried — stop the server, unload models,
    settings, quit — is on the right-click menu and in the tray menu.
    """

    hideRequested = Signal()
    stopRequested = Signal()
    unloadRequested = Signal()
    settingsRequested = Signal()
    quitRequested = Signal()

    def __init__(self, settings: Settings, hub: StatusHub):
        super().__init__()
        self._settings = settings
        self._hub = hub
        self._claude_card = None
        self._claude_status = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self._drag_pos = None
        self._user_moved = False  # once dragged, stop auto-snapping to the corner
        self._minimized = False
        self._user_hidden = False  # hidden via ×: don't let a poll re-show it
        self._present = False      # currently faded in because there's content
        self._forced_visible = False  # shown from the tray while idle: don't auto-hide it
        self._hide_connected = False  # tracked explicitly to avoid disconnecting an unconnected signal
        self._stop_armed = False
        self._cards: dict = {}
        self._collapsed: set = set()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.setSizeConstraint(QLayout.SetFixedSize)

        # The overlay's own window buttons. They have no bar of their own:
        # _attach_window_controls() moves them into the top card's header.
        self.window_controls = WindowControls()
        self.window_controls.minimizeToggled.connect(self.toggle_minimize)
        self.window_controls.closeClicked.connect(lambda: self.hideRequested.emit())

        # Right-click anywhere on the overlay for what the header strip used to
        # carry as buttons.
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        self.claude_box = QWidget()
        claude_layout = QVBoxLayout(self.claude_box)
        claude_layout.setContentsMargins(0, 0, 0, 0)
        claude_layout.setSpacing(0)
        self.claude_layout = claude_layout

        self.cards_box = QWidget()
        self.cards_layout = QBoxLayout(QBoxLayout.TopToBottom, self.cards_box)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(6)
        self.cards_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        outer.addWidget(self.cards_box)
        outer.addWidget(self.claude_box)

        self._opacity_anim = QPropertyAnimation(self, b"windowOpacity")
        self.setWindowOpacity(0.0)

        # Countdowns are interpolated locally so they tick once a second rather
        # than in poll-sized jumps.
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick_countdowns)
        self._tick_timer.start(1000)

        hub.updated.connect(self._on_hub_updated)
        hub.removed.connect(lambda _hid: self.sync_cards())
        settings.changed.connect(self.apply_settings)
        self.apply_settings()

    # ---- header controls -------------------------------------------------- #

    def _show_context_menu(self, pos):
        """What the header strip's buttons used to do, on right-click."""
        menu = QMenu(self)
        menu.addAction("Settings & dashboard…", lambda: self.settingsRequested.emit())
        menu.addSeparator()
        menu.addAction("Unload local models", lambda: self.unloadRequested.emit())
        stop = menu.addAction("Stop local Ollama server…",
                              lambda: self._confirm_stop())
        stop.setToolTip("Unloads the models and kills the Ollama server")
        menu.addSeparator()
        menu.addAction("Collapse" if not self._minimized else "Expand", self.toggle_minimize)
        menu.addAction("Hide overlay", lambda: self.hideRequested.emit())
        menu.addAction("Quit overlay", lambda: self.quitRequested.emit())
        menu.exec(self.mapToGlobal(pos))

    def _confirm_stop(self):
        """Stopping the server ends every warm model, so it asks first.

        The old header button armed itself on a first click; from a menu a
        plain confirmation dialog is clearer and costs the same one extra click.
        """
        answer = QMessageBox.question(
            self, "Stop Ollama?",
            "Unload the local models and stop the Ollama server?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.stopRequested.emit()

    def apply_settings(self):
        look = self._settings.section("appearance")
        theme.set_state_colors(look.get("state_active"), look.get("state_waiting"),
                               look.get("state_stopped"))
        horizontal = look.get("layout") == "horizontal"
        self.cards_layout.setDirection(QBoxLayout.LeftToRight if horizontal else QBoxLayout.TopToBottom)
        self.sync_cards()

    def _wanted_cards(self) -> list:
        wanted = []
        if self._settings.section("local").get("show", True):
            wanted.append(LOCAL_ID)
        wanted += [h.id for h in self._settings.hosts() if h.enabled and h.show_in_overlay]
        return wanted

    def sync_cards(self):
        look = self._settings.section("appearance")
        wanted = self._wanted_cards()
        for host_id in [hid for hid in self._cards if hid not in wanted]:
            card = self._cards.pop(host_id)
            self.cards_layout.removeWidget(card)
            card.deleteLater()

        for index, host_id in enumerate(wanted):
            card = self._cards.get(host_id)
            if card is None:
                card = HostCard(host_id, width=int(look.get("width", 272)))
                card.collapseToggled.connect(self._on_card_collapsed)
                # A card rebuilt while the overlay is minimised must come back
                # collapsed, or a settings change would silently un-minimise it.
                card.set_collapsed(self._minimized or host_id in self._collapsed)
                self._cards[host_id] = card
            self.cards_layout.insertWidget(index, card, 0, Qt.AlignTop | Qt.AlignLeft)
            ident = self._settings.identity(host_id)
            card.set_identity(ident.name, ident.color, ident.kind, ident.kind_color, ident.tags)
            card.set_appearance(
                width=int(look.get("width", 272)),
                border_width=int(look.get("border_width", 1)),
                radius=int(look.get("border_radius", 14)),
                alpha=int(look.get("opacity", 175)),
                max_rows=int(look.get("max_rows", 6)),
                show_gpu=bool(look.get("show_gpu", True)),
            )
            self._refresh_card(host_id)
        self.sync_claude_card()
        self._attach_window_controls()
        self._after_update()

    def sync_claude_card(self):
        """Create, update or drop the Claude card to match the setting."""
        conf = self._settings.section("claude")
        look = self._settings.section("appearance")
        if not conf.get("show", True):
            if self._claude_card is not None:
                self.claude_layout.removeWidget(self._claude_card)
                self._claude_card.deleteLater()
                self._claude_card = None
            self.claude_box.setVisible(False)
            return

        if self._claude_card is None:
            self._claude_card = ClaudeCard(width=int(look.get("width", 272)))
            self.claude_layout.addWidget(self._claude_card)
        self.claude_box.setVisible(True)
        self._claude_card.set_appearance(
            width=int(look.get("width", 272)),
            border_width=int(look.get("border_width", 1)),
            radius=int(look.get("border_radius", 14)),
            alpha=int(look.get("opacity", 175)),
            colour=conf.get("color", "#D97757"),
            max_sessions=int(conf.get("max_sessions", 4)),
        )
        if self._claude_status is not None:
            self._claude_card.set_status(self._claude_status)
        self._claude_card.set_collapsed(self._minimized)
        self._update_claude_visibility()

    def _models_loaded(self) -> bool:
        """Is any shown host actually holding a model in memory?

        STOPPED rows don't count: a pinned model that isn't loaded, or one still
        lingering on the roster after being unloaded, is not Ollama working.
        """
        for host_id in self._cards:
            status = self._hub.status(host_id)
            if status is None or not status.reachable:
                continue
            if any(row.state is not ModelState.STOPPED for row in status.rows):
                return True
        return False

    def _claude_visible(self) -> bool:
        conf = self._settings.section("claude")
        if not conf.get("show", True) or self._claude_card is None:
            return False
        if conf.get("only_when_idle", True) and self._models_loaded():
            return False
        return True

    def _update_claude_visibility(self):
        """Re-evaluated on every hub update, so the card steps aside the moment
        a model loads and comes back when the last one goes."""
        self.claude_box.setVisible(self._claude_visible())

    def _all_cards(self) -> list:
        cards = list(self._cards.values())
        if self._claude_card is not None:
            cards.append(self._claude_card)
        return cards

    def _visible_cards(self) -> list:
        """Cards actually on screen, top first — the controls must land on one
        of these, never on a card that is hidden."""
        cards = [self._cards[hid] for hid in self._wanted_cards() if hid in self._cards]
        if self._claude_visible():
            cards.append(self._claude_card)
        return cards

    def _attach_window_controls(self):
        """Move the minimise/close buttons into whichever card is on top.

        Re-parenting is how a single instance follows the top card around:
        addWidget() takes ownership away from the previous header, so the
        buttons can never end up drawn twice.
        """
        visible = self._visible_cards()
        top = visible[0] if visible else None
        if top is not None:
            top.set_window_controls(self.window_controls)
        else:
            self.window_controls.setParent(None)

    def on_claude_status(self, status):
        self._claude_status = status
        if self._claude_card is not None:
            self._claude_card.set_status(status)
            self._resize_to_content()
        self._update_presence()

    def _claude_is_busy(self) -> bool:
        conf = self._settings.section("claude")
        if not (conf.get("show_when_busy", True) and self._claude_visible()):
            return False
        return bool(self._claude_status and self._claude_status.busy_sessions)

    def _on_card_collapsed(self, host_id: str, collapsed: bool):
        if collapsed:
            self._collapsed.add(host_id)
        else:
            self._collapsed.discard(host_id)
        self._resize_to_content()

    def _refresh_card(self, host_id: str):
        card = self._cards.get(host_id)
        if card is None:
            return
        status = self._hub.status(host_id)
        if status is None:
            card.set_status([], None, reachable=host_id == LOCAL_ID,
                            status_text="", message="connecting…")
            return
        message = "" if status.reachable else status.error
        card.set_status(status.rows, status.gpu, reachable=status.reachable,
                        status_text=status_text_for(status), message=message,
                        ollama_online=status.ollama_online)

    def _on_hub_updated(self, host_id: str):
        self._refresh_card(host_id)
        self._after_update()

    def _after_update(self):
        # Models may have just loaded or cleared, which is what decides whether
        # the Claude card belongs on screen.
        self._update_claude_visibility()
        self._attach_window_controls()
        # The aggregate state the header dot used to show now rides on the tray
        # icon, which TrayController recolours from the same hub data, and on
        # each card's own dot.
        self._update_presence()
        self._resize_to_content()

    def _update_presence(self):
        """Fade in while there's anything to show, out when there isn't.

        "Anything" is any local model row, or any remote host card at all — a
        remote server being *offline* is exactly the thing worth seeing.
        """
        auto_hide = self._settings.section("behavior").get("auto_hide_when_idle", True)
        if not self._visible_cards():
            # Every card switched off: there is nothing to draw and no window
            # buttons to draw it with, so don't float an empty frame.
            content = False
        else:
            content = (not auto_hide) or self._claude_is_busy() or any(
                hid != LOCAL_ID or card.has_rows for hid, card in self._cards.items()
            )
        if content:
            self._forced_visible = False
        if content and not self._present:
            self._present = True
            self.fade_in()
        elif not content and self._present and not self._forced_visible:
            self._present = False
            self.fade_out()

    def _tick_countdowns(self):
        if self._minimized or not self.isVisible():
            return
        for card in self._cards.values():
            card.tick()

    # ---- geometry ----------------------------------------------------------- #

    def _resize_to_content(self):
        # Collapsing a card hides its body, which drops the card's sizeHint but
        # does not by itself shrink the card: the nested layouts have to be
        # re-activated from the inside out, or the overlay keeps the dead
        # vertical space where the body used to be.
        for card in self._all_cards():
            card.layout().activate()
            card.adjustSize()
        for box in (self.cards_box, self.claude_box):
            if box.layout() is not None:
                box.layout().activate()
            box.adjustSize()
        self.layout().activate()
        self.adjustSize()
        if not self._user_moved:
            self._position_in_corner()

    def _position_in_corner(self, margin=20):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        corner = self._settings.section("appearance").get("corner", "top-right")
        x = screen.left() + margin if "left" in corner else screen.right() - self.width() - margin
        y = screen.bottom() - self.height() - margin if "bottom" in corner else screen.top() + margin
        self.move(x, y)

    def toggle_minimize(self):
        """Collapse every card to its header row, and back.

        With no title bar left to shrink to, collapsing hides each card's body
        instead of the whole stack — which keeps the window buttons, and each
        card's own name and status, on screen while minimised.
        """
        self._minimized = not self._minimized
        self.window_controls.set_minimized(self._minimized)
        for card in self._all_cards():
            card.set_collapsed(self._minimized)
        self._resize_to_content()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            self._user_moved = True
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = None
            event.accept()

    def mouseDoubleClickEvent(self, event):
        # Double-click snaps a dragged HUD back to its configured corner.
        if event.button() == Qt.LeftButton:
            self._user_moved = False
            self._position_in_corner()
            event.accept()

    # ---- fading ----------------------------------------------------------- #

    def _disconnect_hide_on_finish(self):
        # Tracked with our own flag rather than relying on disconnect()'s
        # success/failure — PySide6 prints a RuntimeWarning to stderr when
        # disconnecting a signal with no matching connection, and under
        # pythonw.exe (no console, sys.stderr=None) that write can itself
        # raise, which is exactly the class of bug that broke this app twice.
        if self._hide_connected:
            self._opacity_anim.finished.disconnect(self.hide)
            self._hide_connected = False

    def fade_in(self):
        if self._user_hidden:
            return  # dismissed with ×: only the tray icon brings it back
        self.show()
        self._opacity_anim.stop()
        # Undo any pending fade_out()'s hide-on-finish, otherwise a fade_out that
        # gets interrupted mid-animation by a fade_in would still hide us right
        # after this animation completes.
        self._disconnect_hide_on_finish()
        self._opacity_anim.setDuration(220)
        self._opacity_anim.setStartValue(self.windowOpacity())
        self._opacity_anim.setEndValue(1.0)
        self._opacity_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._opacity_anim.start()

    def fade_out(self):
        self._opacity_anim.stop()
        # Disconnect before reconnecting — otherwise each fade_out() over a long
        # session adds another duplicate connection to the same animation object.
        self._disconnect_hide_on_finish()
        self._opacity_anim.setDuration(350)
        self._opacity_anim.setStartValue(self.windowOpacity())
        self._opacity_anim.setEndValue(0.0)
        self._opacity_anim.setEasingCurve(QEasingCurve.InCubic)
        self._opacity_anim.finished.connect(self.hide)
        self._hide_connected = True
        self._opacity_anim.start()

    def hide_to_tray(self):
        self._user_hidden = True
        self.fade_out()

    def restore_from_tray(self):
        self._user_hidden = False
        self._present = True
        self._forced_visible = True
        self.fade_in()

    @property
    def user_hidden(self) -> bool:
        return self._user_hidden


def make_tray_icon(colour: str) -> QIcon:
    """Build the tray glyph in code — a filled dot in the aggregate state colour
    — so the app needs no image assets on disk."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(QColor(24, 24, 27, 230)))
    painter.drawRoundedRect(1, 1, 30, 30, 8, 8)
    painter.setBrush(QBrush(QColor(colour)))
    painter.drawEllipse(9, 9, 14, 14)
    painter.end()
    return QIcon(pixmap)


class Toast(QWidget):
    """Bottom-corner toast notification."""

    def __init__(self, settings: Settings):
        super().__init__()
        self._settings = settings
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(320, 92)

        card = QFrame(self)
        card.setGeometry(0, 0, 320, 92)
        card.setStyleSheet(f"""
            QFrame {{
                background: rgba(32, 33, 36, 245);
                border: 1px solid {BORDER};
                border-radius: 10px;
            }}
        """)

        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        self.accent_bar = QFrame()
        self.accent_bar.setFixedWidth(4)
        self.accent_bar.setStyleSheet(f"background: {ACCENT}; border-radius: 2px;")
        layout.addWidget(self.accent_bar)

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        self.title = QLabel("Ollama model loaded")
        self.title.setStyleSheet("color: white; border: none;")
        self.title.setFont(mono(11, QFont.DemiBold))
        self.subtitle = QLabel("llama3.1:8b")
        self.subtitle.setStyleSheet(f"color: {LABEL_GRAY}; border: none;")
        self.subtitle.setFont(mono(10))
        self.subtitle.setWordWrap(True)
        text_col.addWidget(self.title)
        text_col.addWidget(self.subtitle)
        text_col.addStretch()
        layout.addLayout(text_col, 1)

        self._opacity_anim = QPropertyAnimation(self, b"windowOpacity")
        self._slide_anim = QPropertyAnimation(self, b"pos")
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._dismiss)
        self._hide_connected = False

        self.setWindowOpacity(0.0)

    def _end_pos(self, margin=20):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        # Bottom-right, unless the HUD lives there — then bottom-left.
        hud_corner = self._settings.section("appearance").get("corner", "top-right")
        if hud_corner == "bottom-right":
            x = screen.left() + margin
        else:
            x = screen.right() - self.width() - margin
        y = screen.bottom() - self.height() - margin
        return QPoint(x, y)

    def _disconnect_hide_on_finish(self):
        # Tracked with our own flag rather than relying on disconnect()'s
        # success/failure — see OverlayHUD._disconnect_hide_on_finish for why.
        if self._hide_connected:
            self._opacity_anim.finished.disconnect(self.hide)
            self._hide_connected = False

    def show_toast(self, title, subtitle, hold_ms=None, colour=ACCENT):
        notes = self._settings.section("notifications")
        if not notes.get("toasts", True):
            return
        if hold_ms is None:
            hold_ms = int(float(notes.get("toast_seconds", 4)) * 1000)
        self.title.setText(title)
        self.subtitle.setText(subtitle)
        self.accent_bar.setStyleSheet(f"background: {colour}; border-radius: 2px;")

        end_pos = self._end_pos()
        start_pos = QPoint(end_pos.x(), end_pos.y() + 30)
        self.move(start_pos)
        self.setWindowOpacity(0.0)
        self.show()

        self._slide_anim.stop()
        self._slide_anim.setDuration(260)
        self._slide_anim.setStartValue(start_pos)
        self._slide_anim.setEndValue(end_pos)
        self._slide_anim.setEasingCurve(QEasingCurve.OutCubic)

        self._opacity_anim.stop()
        # Undo any pending _dismiss()'s hide-on-finish — otherwise a dismiss
        # interrupted mid-animation by a new toast would hide this new one
        # right after it finishes fading in.
        self._disconnect_hide_on_finish()
        self._opacity_anim.setDuration(260)
        self._opacity_anim.setStartValue(0.0)
        self._opacity_anim.setEndValue(1.0)

        self._slide_anim.start()
        self._opacity_anim.start()

        self._hide_timer.start(hold_ms)

    def _dismiss(self):
        self._opacity_anim.stop()
        # Disconnect before reconnecting — otherwise each dismiss over a long
        # session adds another duplicate connection to the same animation object.
        self._disconnect_hide_on_finish()
        self._opacity_anim.setDuration(400)
        self._opacity_anim.setStartValue(self.windowOpacity())
        self._opacity_anim.setEndValue(0.0)
        self._opacity_anim.finished.connect(self.hide)
        self._hide_connected = True
        self._opacity_anim.start()


class TrayController:
    """Notification-area icon: the way back after × hides the overlay.

    Windows puts newly-registered icons under the "hidden icons" chevron by
    default; drag it onto the taskbar itself if you'd rather have it always
    visible. On GNOME the tray needs the AppIndicator extension. The icon's
    colour mirrors the header dot, so aggregate state is readable without
    opening anything.
    """

    def __init__(self, app, overlay, hub, controller, open_settings, quit_app):
        self._app = app
        self._overlay = overlay
        self._hub = hub
        self._controller = controller
        self._quit_app = quit_app
        self._colour = None
        self._warned_hidden = False

        self.tray = QSystemTrayIcon(make_tray_icon(IDLE_GREY), app)
        self.tray.setToolTip(APP_NAME)

        menu = QMenu()
        self._toggle_action = QAction("Hide overlay", menu)
        self._toggle_action.triggered.connect(self._toggle_overlay)
        menu.addAction(self._toggle_action)
        settings_action = QAction("Settings && dashboard…", menu)
        settings_action.triggered.connect(open_settings)
        menu.addAction(settings_action)
        menu.addSeparator()

        unload_action = QAction("Unload local models (keep server)", menu)
        unload_action.triggered.connect(self._controller.unload_async)
        menu.addAction(unload_action)

        stop_action = QAction("Stop local Ollama server", menu)
        stop_action.triggered.connect(self._controller.stop_async)
        menu.addAction(stop_action)
        menu.addSeparator()

        quit_action = QAction("Quit overlay", menu)
        quit_action.triggered.connect(self.quit)
        menu.addAction(quit_action)

        self._menu = menu  # kept referenced: a GC'd QMenu takes the tray menu with it
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()
        hub.updated.connect(self.on_hub_updated)

    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._toggle_overlay()

    def _toggle_overlay(self):
        if self._overlay.user_hidden or not self._overlay.isVisible():
            self._overlay.restore_from_tray()
        else:
            self._overlay.hide_to_tray()
        self.sync_toggle_text()

    def sync_toggle_text(self):
        hidden = self._overlay.user_hidden or not self._overlay.isVisible()
        self._toggle_action.setText("Show overlay" if hidden else "Hide overlay")

    def on_hub_updated(self, _host_id=None):
        rows = self._hub.all_rows()
        colour = theme.aggregate_colour(rows)
        if colour != self._colour:
            self._colour = colour
            self.tray.setIcon(make_tray_icon(colour))
        statuses = self._hub.all()
        remote = [s for hid, s in statuses.items() if hid != LOCAL_ID]
        active = sum(1 for s in rows if s.state is ModelState.ACTIVE)
        waiting = sum(1 for s in rows if s.state is ModelState.WAITING)
        tip = f"{APP_NAME} — {active} active, {waiting} waiting"
        if remote:
            tip += f"\n{sum(1 for s in remote if s.reachable)}/{len(remote)} remote hosts up"
        self.tray.setToolTip(tip)
        self.sync_toggle_text()

    def note_hidden(self):
        """Explain where the overlay went, once per session."""
        if self._warned_hidden:
            return
        self._warned_hidden = True
        try:
            self.tray.showMessage(
                f"{APP_NAME} hidden",
                "Click the tray icon to bring it back, or right-click for settings/quit.",
                make_tray_icon(self._colour or IDLE_GREY),
                4000,
            )
        except Exception:
            pass  # balloon tips can be disabled by policy; not worth failing over

    def quit(self):
        self.tray.hide()
        self._quit_app()


SINGLE_INSTANCE_KEY = "ollama-overlay-pro"


def acquire_single_instance(name: str = SINGLE_INSTANCE_KEY, message: bytes = b"show-settings"):
    """Return a listening QLocalServer, or None if another instance holds it.

    Two copies of the overlay means two stacked always-on-top windows and two
    tray icons fighting over the same state, which is confusing enough to look
    like a bug. Easy to end up with by accident: an autostart entry plus one
    manual launch. A second launch therefore asks the running instance to open
    its settings window (so the Start-menu shortcut still "does something") and
    exits. The server object must stay referenced for the lock to hold.
    """
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(200):
        probe.write(message + b"\n")
        probe.flush()
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return None
    # A crashed instance can leave the socket file behind on Unix; removing a
    # name nobody is listening on is safe, and the connect attempt above already
    # proved no live instance owns it.
    QLocalServer.removeServer(name)
    server = QLocalServer()
    return server if server.listen(name) else None


class SharingManager:
    """Keeps the local passthrough server + LAN beacon matching Settings."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self.server = StatusServer()
        self.beacon = BeaconSender()

    def apply(self):
        cfg = self._settings.section("sharing")
        if cfg.get("enabled"):
            ok = self.server.start(cfg.get("bind", "0.0.0.0"), int(cfg.get("port", 11480)),
                                   cfg.get("token", ""))
            if ok and cfg.get("beacon", True):
                self.beacon.start(self._settings.local_name(), int(cfg.get("port", 11480)),
                                  bool(cfg.get("token")))
            else:
                self.beacon.stop()
        else:
            self.server.stop()
            self.beacon.stop()

    def stop(self):
        self.server.stop()
        self.beacon.stop()


def parse_args(argv):
    parser = argparse.ArgumentParser(description=f"{APP_NAME} — Ollama HUD")
    parser.add_argument("--settings", action="store_true",
                        help="open the settings & dashboard window on start")
    parser.add_argument("--autostart", choices=["on", "off"],
                        help="enable/disable start at login, then exit")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.autostart:
        ok, message = platform_support.set_autostart(args.autostart == "on", "overlay")
        print(message)
        return 0 if ok else 1

    platform_support.prefer_xcb_on_wayland()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    app.setPalette(theme.dark_palette())

    instance_lock = acquire_single_instance()
    if instance_lock is None:
        # Nothing is printed: under pythonw.exe there's no console to print to.
        return 0
    # The overlay lives in the tray, so hiding the last window must not end the
    # process — quitting is an explicit choice from the tray menu.
    app.setQuitOnLastWindowClosed(False)

    settings = Settings()
    hub = StatusHub()
    overlay = OverlayHUD(settings, hub)
    toast = Toast(settings)
    monitor = OllamaMonitor(gpu_reader=get_gpu_reader(),
                            base_url=settings.data.get("ollama_url") or "http://127.0.0.1:11434",
                            poll_ms=int(settings.data.get("poll_interval_ms", 1500)),
                            pinned=settings.pinned_models())
    controller = OllamaController(base_url=monitor.base_url)
    poller = RemotePoller()
    sharing = SharingManager(settings)
    settings_window = {"w": None}

    def open_settings():
        if settings_window["w"] is None:
            from settings_window import SettingsWindow  # imported lazily: only built when asked for
            settings_window["w"] = SettingsWindow(settings, hub, poller, sharing, controller, overlay)
        win = settings_window["w"]
        win.show()
        win.raise_()
        win.activateWindow()

    def on_local_roster(rows, gpu):
        hub.set_local(rows, gpu, monitor.ollama_online, monitor.ollama_version,
                      monitor.base_url, settings.local_name())
        if sharing.server.running:
            sharing.server.update(build_snapshot(
                settings.local_name(), rows, gpu, monitor.base_url,
                monitor.ollama_online, monitor.ollama_version))

    monitor.rosterChanged.connect(on_local_roster)
    poller.statusUpdated.connect(hub.set_remote)
    poller.hostRemoved.connect(hub.forget)

    local_colour = lambda: settings.identity(LOCAL_ID).color  # noqa: E731
    monitor.modelLoaded.connect(
        lambda names: toast.show_toast("Ollama model loaded", ", ".join(names), colour=local_colour())
    )
    monitor.modelSwitched.connect(
        lambda removed, added: toast.show_toast(
            "Ollama switched model", f"{', '.join(removed)} -> {', '.join(added)}",
            colour=local_colour())
    )
    monitor.modelUnloaded.connect(
        lambda removed: toast.show_toast(
            "Ollama model stopped", ", ".join(removed), colour="#8b8b93"
        )
    )

    def on_reachability(host_id, reachable):
        if not settings.section("notifications").get("host_offline_toasts", True):
            return
        ident = settings.identity(host_id)
        status = hub.status(host_id)
        if reachable:
            toast.show_toast(f"{ident.name} is back online", status.url if status else "",
                             colour=ident.color)
        else:
            toast.show_toast(f"{ident.name} went offline",
                             (status.error if status else "") or "unreachable", colour=DANGER)

    def on_remote_models(host_id, loaded, unloaded):
        if not settings.section("notifications").get("remote_model_toasts", False):
            return
        ident = settings.identity(host_id)
        if loaded:
            toast.show_toast(f"{ident.name}: model loaded", ", ".join(loaded), colour=ident.color)
        elif unloaded:
            toast.show_toast(f"{ident.name}: model stopped", ", ".join(unloaded), colour="#8b8b93")

    hub.reachabilityChanged.connect(on_reachability)
    hub.remoteModelsChanged.connect(on_remote_models)

    def apply_runtime_settings():
        monitor.set_poll_interval(int(settings.data.get("poll_interval_ms", 1500)))
        monitor.set_pinned(settings.pinned_models())
        poller.set_interval(float(settings.data.get("remote_poll_interval_s", 3)))
        poller.set_hosts(settings.hosts())
        sharing.apply()
        # Turning the Claude card off stops its watcher thread too: no point
        # walking the process table and re-reading transcripts for a hidden card.
        if settings.section("claude").get("show", True):
            claude_watcher.start()
        else:
            claude_watcher.stop()

    settings.changed.connect(apply_runtime_settings)

    def quit_app():
        for stopper in (monitor.stop, poller.stop, sharing.stop, claude_watcher.stop):
            try:
                stopper()
            except Exception:
                log_exception("quit_app")
        app.quit()

    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = TrayController(app, overlay, hub, controller, open_settings, quit_app)

        def on_hide_requested():
            overlay.hide_to_tray()
            tray.note_hidden()
            tray.sync_toggle_text()

        overlay.hideRequested.connect(on_hide_requested)
    else:
        # No notification area (some minimal Linux WMs, GNOME without the
        # AppIndicator extension): × has to mean quit, otherwise it would hide
        # the window with no way to get it back.
        overlay.window_controls.close_btn.setToolTip(
            "Quit the overlay (no system tray available)")
        overlay.hideRequested.connect(quit_app)

    overlay.settingsRequested.connect(open_settings)
    overlay.stopRequested.connect(controller.stop_async)
    overlay.unloadRequested.connect(controller.unload_async)
    overlay.quitRequested.connect(quit_app)
    controller.stopFinished.connect(
        lambda ok, message: toast.show_toast(
            "Ollama stopped" if ok else "Couldn't stop Ollama",
            message,
            colour=ACCENT if ok else DANGER,
        )
    )
    controller.unloadFinished.connect(
        lambda count: toast.show_toast(
            "Models unloaded" if count else "Nothing to unload",
            f"{count} model(s) released from VRAM" if count else "No models were resident",
            colour=ACCENT if count else "#8b8b93",
        )
    )

    def on_instance_message():
        conn = instance_lock.nextPendingConnection()
        if conn is None:
            return
        conn.waitForReadyRead(300)
        if b"show-settings" in bytes(conn.readAll()):
            overlay.restore_from_tray()
            open_settings()
        conn.disconnectFromServer()

    instance_lock.newConnection.connect(on_instance_message)

    claude_watcher = ClaudeWatcher(
        usage_refresh_s=float(settings.section("claude").get("usage_refresh_s", 15) or 15))
    claude_watcher.updated.connect(overlay.on_claude_status)
    if settings.section("claude").get("show", True):
        claude_watcher.start()

    apply_runtime_settings()
    monitor.start()
    poller.start()
    if args.settings or not settings.path.exists():
        # First run (no config yet): open settings so the new features are discoverable.
        settings.save()
        QTimer.singleShot(400, open_settings)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
