#!/usr/bin/env bash
# Local AI Overlay Pro installer for Linux.
#
#   Overlay (desktop HUD + settings/dashboard), as your normal user:
#     curl -fsSL https://raw.githubusercontent.com/nomad9021/Local-AI-Overlay-Pro/main/install/install.sh | bash
#
#   Agent only (headless servers — shares this machine's Ollama status):
#     curl -fsSL https://raw.githubusercontent.com/nomad9021/Local-AI-Overlay-Pro/main/install/install.sh | bash -s -- --agent
#
#   Uninstall:
#     curl -fsSL https://raw.githubusercontent.com/nomad9021/Local-AI-Overlay-Pro/main/install/install.sh | bash -s -- --uninstall
#
# Options: --agent  --uninstall  --system  --token <t>  --port <n>  --no-autostart
#          --no-deps  --branch <b>  --purge (with --uninstall: also delete settings)
#
# As a normal user everything goes under ~/.local and ~/.config; sudo is used
# only to install system packages (Python venv support, Qt's X11 libraries) and
# to open the firewall port. The agent runs as a systemd --user service with
# lingering enabled, so it keeps running with nobody logged in.
# With --system (or when run as root) the agent instead installs to /opt, runs
# as a dedicated unprivileged 'laio-agent' user under a system service, and
# keeps its config (with the token, mode 0600) in /etc/local-ai-overlay-pro.
set -euo pipefail

REPO="nomad9021/Local-AI-Overlay-Pro"
APP_NAME="Local AI Overlay Pro"
APP_ID="local-ai-overlay-pro"
UNIT="local-ai-overlay-agent.service"
SERVICE_USER="laio-agent"

MODE="overlay"
ACTION="install"
AUTOSTART=1
TOKEN=""
TOKEN_SET=0
PORT=11480
SYSTEM_WIDE=0
NO_DEPS=0
PURGE=0
BRANCH="${LAIO_BRANCH:-main}"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --agent) MODE="agent" ;;
        --uninstall) ACTION="uninstall" ;;
        --system) SYSTEM_WIDE=1 ;;
        --token) [ $# -ge 2 ] || die "--token needs a value"; TOKEN="$2"; TOKEN_SET=1; shift ;;
        --port) [ $# -ge 2 ] || die "--port needs a value"; PORT="$2"; shift ;;
        --branch) [ $# -ge 2 ] || die "--branch needs a value"; BRANCH="$2"; shift ;;
        --no-autostart) AUTOSTART=0 ;;
        --no-deps) NO_DEPS=1 ;;
        --purge) PURGE=1 ;;
        -h|--help) sed -n '2,24p' "$0" 2>/dev/null || true; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
done

case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
[ "$(uname -s)" = "Linux" ] || die "this installer is for Linux; on Windows use install.ps1"

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
    if [ "$MODE" = "agent" ]; then
        SYSTEM_WIDE=1
    elif [ "$ACTION" = "install" ]; then
        die "install the overlay as your normal desktop user, not root (sudo is used only where needed)"
    fi
elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    SUDO=""
fi

if [ "$SYSTEM_WIDE" = 1 ]; then
    PREFIX="/opt/$APP_ID"
    CONF_DIR="/etc/$APP_ID"
else
    PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_ID"
    CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_ID"
fi
APP_DIR="$PREFIX/app"
VENV="$PREFIX/venv"
PY="$VENV/bin/python"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/local-ai-overlay"
DESKTOP_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/$APP_ID.desktop"
AGENT_CONF="$CONF_DIR/agent.json"

as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"
    elif [ -n "$SUDO" ]; then $SUDO "$@"
    else return 1
    fi
}

# --------------------------------------------------------------------------- #
# Packages
# --------------------------------------------------------------------------- #
install_packages() {
    [ "$NO_DEPS" = 1 ] && return 0
    local base gui pm
    if command -v apt-get >/dev/null 2>&1; then
        pm="apt"
        base="python3 python3-venv python3-pip curl tar ca-certificates"
        gui="libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libfontconfig1 libdbus-1-3 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxcb-xinerama0 libxcb-randr0 libxcb-render-util0 libxcb-image0 libxcb-xkb1"
    elif command -v dnf >/dev/null 2>&1; then
        pm="dnf"
        base="python3 python3-pip curl tar"
        gui="xcb-util-cursor libxkbcommon-x11 mesa-libEGL mesa-libGL fontconfig dbus-libs xcb-util-wm xcb-util-keysyms xcb-util-image xcb-util-renderutil"
    elif command -v pacman >/dev/null 2>&1; then
        pm="pacman"
        base="python python-pip curl tar"
        gui="xcb-util-cursor libxkbcommon-x11 libglvnd fontconfig dbus xcb-util-wm xcb-util-keysyms xcb-util-image xcb-util-renderutil"
    elif command -v zypper >/dev/null 2>&1; then
        pm="zypper"
        base="python3 python3-pip curl tar"
        gui="libxcb-cursor0 libxkbcommon-x11-0 libEGL1 libGL1 fontconfig libdbus-1-3 libxcb-icccm4 libxcb-keysyms1 libxcb-image0 libxcb-render-util0"
    else
        warn "unknown package manager — make sure python3 (3.9+) with venv support is installed"
        return 0
    fi
    [ "$MODE" = "agent" ] && gui=""   # the agent is headless: no X11/GL libraries needed

    if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then
        warn "no sudo: skipping system packages ($base $gui)"
        return 0
    fi
    say "Installing system packages with $pm (may ask for your password)"
    case "$pm" in
        apt)
            as_root env DEBIAN_FRONTEND=noninteractive apt-get update -qq || true
            # shellcheck disable=SC2086
            as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $base
            if [ -n "$gui" ]; then
                # shellcheck disable=SC2086
                as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $gui 2>/dev/null || {
                    # Older releases lack one or two of these; take what exists.
                    for p in $gui; do as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$p" >/dev/null 2>&1 || warn "package $p unavailable"; done
                }
            fi ;;
        dnf)
            # shellcheck disable=SC2086
            as_root dnf install -y -q $base $gui || as_root dnf install -y -q $base ;;
        pacman)
            # shellcheck disable=SC2086
            as_root pacman -Sy --needed --noconfirm $base $gui ;;
        zypper)
            # shellcheck disable=SC2086
            as_root zypper --non-interactive install $base $gui || as_root zypper --non-interactive install $base ;;
    esac
}

find_python() {
    for candidate in python3 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

fetch_source() {
    local here=""
    # Only when run as a file from a checkout (./install/install.sh); when
    # piped from curl there's no script path and the download is used.
    if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
        here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fi
    if [ -n "$here" ] && [ -f "$here/../app.py" ]; then
        SRC="$(cd "$here/.." && pwd)"
        say "Using local source at $SRC"
        return 0
    fi
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    local url="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
    say "Downloading $url"
    curl -fsSL "$url" | tar -xz -C "$TMP"
    SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)"
    [ -f "$SRC/app.py" ] || die "downloaded archive doesn't look right"
}

stop_running() {
    pkill -f "$APP_DIR/app.py" 2>/dev/null || true
    pkill -f "$APP_DIR/agent.py" 2>/dev/null || true
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user stop "$UNIT" 2>/dev/null || true
        [ "$SYSTEM_WIDE" = 1 ] && as_root systemctl stop "$UNIT" 2>/dev/null || true
    fi
}

install_files() {
    local python
    python="$(find_python)" || die "Python 3.9+ not found — install python3 and python3-venv, then re-run"
    say "Using $python ($("$python" -c 'import sys; print(sys.version.split()[0])'))"

    stop_running
    fetch_source
    if [ "$SYSTEM_WIDE" = 1 ]; then as_root mkdir -p "$PREFIX"; else mkdir -p "$PREFIX"; fi
    as_root_or_me rm -rf "$APP_DIR"
    as_root_or_me mkdir -p "$APP_DIR"
    as_root_or_me cp -a "$SRC/." "$APP_DIR/"
    as_root_or_me rm -rf "$APP_DIR/.git" "$APP_DIR/.venv" "$APP_DIR/__pycache__"

    if ! { [ -x "$PY" ] && "$PY" -m pip --version >/dev/null 2>&1; }; then
        # Missing, or half-created by an earlier run that lacked python3-venv.
        as_root_or_me rm -rf "$VENV"
        say "Creating virtual environment"
        as_root_or_me "$python" -m venv "$VENV" || {
            as_root_or_me rm -rf "$VENV"
            die "couldn't create a venv — install your distro's python3-venv package and re-run"
        }
    fi
    say "Installing Python packages (PySide6, requests, psutil, nvidia-ml-py)..."
    as_root_or_me "$PY" -m pip install --upgrade --disable-pip-version-check -q pip
    as_root_or_me "$PY" -m pip install --disable-pip-version-check --progress-bar off -r "$APP_DIR/requirements.txt" ||
        die "pip install failed (PySide6 needs x86_64/aarch64 and glibc 2.28+)"
}

as_root_or_me() {
    if [ "$SYSTEM_WIDE" = 1 ]; then as_root "$@"; else "$@"; fi
}

open_firewall() {
    local port="$1"
    if command -v ufw >/dev/null 2>&1 && as_root ufw status 2>/dev/null | grep -q "Status: active"; then
        as_root ufw allow "$port/tcp" >/dev/null && say "ufw: allowed $port/tcp"
    fi
    if command -v firewall-cmd >/dev/null 2>&1 && as_root firewall-cmd --state >/dev/null 2>&1; then
        as_root firewall-cmd --permanent --add-port="$port/tcp" >/dev/null &&
            as_root firewall-cmd --reload >/dev/null && say "firewalld: opened $port/tcp"
    fi
}

lan_ip() {
    local ip=""
    ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") {print $(i+1); exit}}')" || true
    [ -n "$ip" ] || ip="$(hostname -I 2>/dev/null | awk '{print $1}')" || true
    [ -n "$ip" ] || ip="$(hostname)"
    echo "$ip"
}

# --------------------------------------------------------------------------- #
# Overlay
# --------------------------------------------------------------------------- #
install_overlay() {
    install_packages
    install_files

    mkdir -p "$BIN_DIR" "$(dirname "$DESKTOP_FILE")"
    cat > "$LAUNCHER" <<EOF
#!/bin/sh
exec "$PY" "$APP_DIR/app.py" "\$@"
EOF
    chmod +x "$LAUNCHER"

    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Ollama status HUD for this and other machines
Exec="$LAUNCHER"
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;Utility;
Actions=Settings;

[Desktop Action Settings]
Name=Settings & Dashboard
Exec="$LAUNCHER" --settings
EOF
    command -v update-desktop-database >/dev/null 2>&1 &&
        update-desktop-database "$(dirname "$DESKTOP_FILE")" >/dev/null 2>&1 || true

    if [ "$AUTOSTART" = 1 ]; then
        "$PY" "$APP_DIR/app.py" --autostart on || warn "couldn't enable autostart"
    fi

    case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR isn't on your PATH — launch from the app menu or run $LAUNCHER" ;; esac

    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        nohup "$LAUNCHER" --settings >/dev/null 2>&1 &
        say "Launched. Settings & dashboard are open; the HUD appears top-right when models are loaded."
    else
        say "Installed. Start it from your app menu ('$APP_NAME') or run: local-ai-overlay"
    fi
    case "${XDG_CURRENT_DESKTOP:-}" in
        *GNOME*)
            warn "GNOME hides tray icons unless the 'AppIndicator and KStatusNotifierItem Support' extension is on;"
            warn "without it, use the gear button on the HUD (x then quits instead of hiding)." ;;
    esac
}

# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
install_agent() {
    install_packages
    install_files

    local token_args=()
    [ "$TOKEN_SET" = 1 ] && token_args=(--token "$TOKEN")

    if [ "$SYSTEM_WIDE" = 1 ]; then
        command -v systemctl >/dev/null 2>&1 || die "--system needs systemd"
        if ! id "$SERVICE_USER" >/dev/null 2>&1; then
            as_root useradd --system --no-create-home --home-dir /nonexistent \
                --shell /usr/sbin/nologin "$SERVICE_USER" 2>/dev/null ||
            as_root useradd -r -M -d /nonexistent -s /sbin/nologin "$SERVICE_USER"
        fi
        # Optional extra access: journald (old-Ollama tok/s fallback) and the
        # render/video groups AMD GPUs need for rocm-smi. Missing groups are fine.
        for g in systemd-journal render video; do
            getent group "$g" >/dev/null 2>&1 && as_root usermod -aG "$g" "$SERVICE_USER" 2>/dev/null || true
        done
        as_root mkdir -p "$CONF_DIR"
        as_root "$PY" "$APP_DIR/agent.py" --config "$AGENT_CONF" --init-config --port "$PORT" ${token_args[@]+"${token_args[@]}"}
        as_root chown "$SERVICE_USER:$SERVICE_USER" "$AGENT_CONF"
        as_root chmod 600 "$AGENT_CONF"

        as_root tee "/etc/systemd/system/$UNIT" >/dev/null <<EOF
[Unit]
Description=$APP_NAME agent (shares this machine's Ollama status)
After=network-online.target ollama.service
Wants=network-online.target

[Service]
User=$SERVICE_USER
ExecStart=$PY $APP_DIR/agent.py --config $AGENT_CONF
Restart=on-failure
RestartSec=5
Environment=XDG_CONFIG_HOME=/var/lib/$APP_ID
StateDirectory=$APP_ID
NoNewPrivileges=yes
ProtectSystem=full
ProtectHome=read-only
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
        as_root systemctl daemon-reload
        as_root systemctl enable --now "$UNIT"
        say "Agent running as system service $UNIT (user $SERVICE_USER)"
    else
        mkdir -p "$CONF_DIR"
        "$PY" "$APP_DIR/agent.py" --config "$AGENT_CONF" --init-config --port "$PORT" ${token_args[@]+"${token_args[@]}"}
        if [ "$AUTOSTART" = 1 ]; then
            if "$PY" "$APP_DIR/agent.py" --config "$AGENT_CONF" --install-service; then
                # Without lingering, user services stop when you log out.
                loginctl enable-linger "$USER" 2>/dev/null || as_root loginctl enable-linger "$USER" 2>/dev/null ||
                    warn "couldn't enable lingering: the agent will only run while you're logged in"
            else
                warn "no systemd user session — starting the agent in the background instead"
                nohup "$PY" "$APP_DIR/agent.py" --config "$AGENT_CONF" >/dev/null 2>&1 &
            fi
        else
            nohup "$PY" "$APP_DIR/agent.py" --config "$AGENT_CONF" >/dev/null 2>&1 &
        fi
    fi

    open_firewall "$PORT"

    local token
    token="$(as_root_or_me "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("token",""))' "$AGENT_CONF" 2>/dev/null || true)"
    echo
    say "Agent installed. On the machine running the overlay: Settings -> Hosts -> Add (or Discover)"
    echo "      Address: $(lan_ip):$PORT"
    echo "      Mode:    Agent"
    echo "      Token:   ${token:-(none)}"
    echo
}

# --------------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------------- #
uninstall() {
    say "Uninstalling $APP_NAME"
    stop_running
    if [ -x "$PY" ] && [ "$SYSTEM_WIDE" = 0 ]; then
        "$PY" "$APP_DIR/app.py" --autostart off >/dev/null 2>&1 || true
        "$PY" "$APP_DIR/agent.py" --config "$AGENT_CONF" --uninstall-service >/dev/null 2>&1 || true
    fi
    rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP_ID.desktop" "$LAUNCHER" "$DESKTOP_FILE"
    if [ -f "/etc/systemd/system/$UNIT" ]; then
        as_root systemctl disable --now "$UNIT" 2>/dev/null || true
        as_root rm -f "/etc/systemd/system/$UNIT"
        as_root systemctl daemon-reload 2>/dev/null || true
    fi
    rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/$APP_ID"
    [ -d "/opt/$APP_ID" ] && as_root rm -rf "/opt/$APP_ID"
    if [ "$PURGE" = 1 ]; then
        rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/$APP_ID"
        [ -d "/etc/$APP_ID" ] && as_root rm -rf "/etc/$APP_ID"
        say "Removed, including settings."
    else
        say "Removed. Settings kept in ${XDG_CONFIG_HOME:-$HOME/.config}/$APP_ID (re-run with --uninstall --purge to delete)."
    fi
}

if [ "$ACTION" = "uninstall" ]; then
    uninstall
elif [ "$MODE" = "agent" ]; then
    say "Installing $APP_NAME agent to $PREFIX"
    install_agent
else
    say "Installing $APP_NAME to $PREFIX"
    install_overlay
fi
