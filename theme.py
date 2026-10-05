"""
Shared look: colours, fonts, and the small formatting helpers that the HUD, the
toast, and the settings/dashboard window all draw with.

State colours are user-configurable (Settings -> Appearance), so they live in a
mutable table here rather than as constants: `set_state_colors()` rewrites it
and every ModelRow picks the new colours up on its next status update.
"""
import hashlib

from PySide6.QtGui import QColor, QFont, QPalette

from model_state import ModelState

ACCENT = "#4ADE80"      # green accent for live values
BORDER = "rgba(255, 255, 255, 25)"
LABEL_GRAY = "#9a9a9a"
DANGER = "#f87171"
IDLE_GREY = "#6B7280"
VRAM_BLUE = "#38bdf8"
MONO_FAMILIES = ["Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Liberation Mono", "monospace"]

DEFAULT_STATE_COLORS = {"active": "#4ADE80", "waiting": "#FBBF24", "stopped": "#8b8b93"}

# ModelState -> (text colour, pill background). Rewritten by set_state_colors().
STATE_STYLE: dict = {}


def rgba(colour: str, alpha: int) -> str:
    c = QColor(colour)
    if not c.isValid():
        c = QColor(LABEL_GRAY)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {int(alpha)})"


def dim(colour: str, factor: int = 170) -> str:
    c = QColor(colour)
    return c.darker(factor).name() if c.isValid() else colour


def valid_colour(colour, fallback: str) -> str:
    return colour if isinstance(colour, str) and QColor(colour).isValid() else fallback


def set_state_colors(active: str, waiting: str, stopped: str) -> None:
    # WAITING defaults to amber rather than green so "warm but idle" can't be
    # mistaken for "working" at a glance.
    active = valid_colour(active, DEFAULT_STATE_COLORS["active"])
    waiting = valid_colour(waiting, DEFAULT_STATE_COLORS["waiting"])
    stopped = valid_colour(stopped, DEFAULT_STATE_COLORS["stopped"])
    STATE_STYLE[ModelState.ACTIVE] = (active, rgba(active, 38))
    STATE_STYLE[ModelState.WAITING] = (waiting, rgba(waiting, 32))
    STATE_STYLE[ModelState.STOPPED] = (stopped, "rgba(255, 255, 255, 18)")


set_state_colors(**DEFAULT_STATE_COLORS)


def state_colour(state) -> str:
    return STATE_STYLE.get(state, STATE_STYLE[ModelState.STOPPED])[0]


def aggregate_colour(rows: list) -> str:
    """Header-dot / tray-icon colour: active colour if anything is generating,
    waiting colour if something is warm, grey if it's all stopped."""
    if any(s.state is ModelState.ACTIVE for s in rows):
        return state_colour(ModelState.ACTIVE)
    if any(s.state is ModelState.WAITING for s in rows):
        return state_colour(ModelState.WAITING)
    return IDLE_GREY


def auto_tag_colour(name: str) -> str:
    """Stable colour for a tag nobody picked a colour for: same name, same hue."""
    digest = hashlib.md5(name.strip().lower().encode("utf-8")).digest()
    colour = QColor.fromHsl(digest[0] * 360 // 256, 170, 150)
    return colour.name()


def pill_style(colour: str, radius: int = 6) -> str:
    return (
        f"color: {colour}; background: {rgba(colour, 38)}; "
        f"border: 1px solid {rgba(colour, 110)}; border-radius: {radius}px; "
        f"padding: 0px 5px;"
    )


def mono(size, weight=QFont.Normal):
    f = QFont()
    f.setFamilies(MONO_FAMILIES)
    f.setPointSize(size)
    f.setWeight(weight)
    return f


def fmt_duration(seconds) -> str:
    """Compact keep_alive countdown: 1h04m / 29:41 / 0:07."""
    if seconds is None:
        return ""
    total = max(0, int(seconds))
    if total >= 3600:
        return f"{total // 3600}h{(total % 3600) // 60:02d}m"
    return f"{total // 60}:{total % 60:02d}"


def dark_palette() -> QPalette:
    """App-wide dark palette, so menus, tooltips and the settings window match
    the HUD instead of falling back to the desktop's light theme."""
    p = QPalette()
    window, base, text = QColor("#141417"), QColor("#1c1c20"), QColor("#e5e5e5")
    p.setColor(QPalette.Window, window)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, QColor("#222227"))
    p.setColor(QPalette.ToolTipBase, QColor("#202024"))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, QColor("#26262b"))
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor(DANGER))
    p.setColor(QPalette.Highlight, QColor("#2f855a"))
    p.setColor(QPalette.HighlightedText, QColor("white"))
    p.setColor(QPalette.PlaceholderText, QColor("#6b6b73"))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor("#6b6b73"))
    return p
