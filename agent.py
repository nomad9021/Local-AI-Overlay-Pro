"""
Headless passthrough agent — run this on any machine (desktop or server, no
display needed) whose Ollama you want to watch from an overlay elsewhere.

It runs the same OllamaMonitor the HUD uses — so remote rows get the real
ACTIVE/WAITING state from the runners' /slots, live tok/s and NVIDIA/AMD GPU
stats — and serves the result read-only over HTTP (see status_server.py),
plus a LAN beacon so overlays can find it with Settings -> Hosts -> Discover.

Configuration lives in a small JSON file (default: <config dir>/agent.json,
created with `--init-config`), so the token never has to appear on a command
line or in a world-readable service unit:

    {"bind": "0.0.0.0", "port": 11480, "token": "...", "beacon": true,
     "name": "", "ollama_url": "http://127.0.0.1:11434", "pinned_models": []}

Usage:
    python agent.py --init-config            # create agent.json with a fresh token
    python agent.py                          # run (Ctrl+C to stop)
    python agent.py --install-service        # Linux: systemd --user unit; Windows: login autostart
    python agent.py --uninstall-service
"""
import argparse
import json
import os
import signal
import sys
from pathlib import Path

import platform_support
from platform_support import APP_NAME, APP_VERSION

DEFAULT_CONFIG = {
    "bind": "0.0.0.0",
    "port": 11480,
    "token": "",
    "beacon": True,
    "name": "",
    "ollama_url": "http://127.0.0.1:11434",
    "pinned_models": [],
}


def default_config_path() -> Path:
    return platform_support.config_dir() / "agent.json"


def load_config(path: Path) -> dict:
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            config.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"warning: couldn't read {path}: {exc}", file=sys.stderr)
    return config


def write_config(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)   # holds the token: owner-only
    os.replace(tmp, path)


def init_config(path: Path, token: str = None, port: int = None, name: str = None) -> dict:
    from status_server import generate_token
    config = load_config(path)
    if token is not None:
        config["token"] = token
    elif not config.get("token"):
        config["token"] = generate_token()
    if port:
        config["port"] = int(port)
    if name is not None:
        config["name"] = name
    write_config(path, config)
    return config


def parse_args(argv):
    p = argparse.ArgumentParser(description=f"{APP_NAME} agent — share this machine's Ollama status")
    p.add_argument("--config", type=Path, default=None, help="path to agent.json")
    p.add_argument("--init-config", action="store_true",
                   help="create/update the config (generating a token if none), print it, exit")
    p.add_argument("--token", help="with --init-config: use this token ('' for none)")
    p.add_argument("--port", type=int, help="override the listen port")
    p.add_argument("--bind", help="override the listen address (default 0.0.0.0)")
    p.add_argument("--name", help="display name announced to overlays (default: hostname)")
    p.add_argument("--install-service", action="store_true",
                   help="start automatically (Linux: systemd --user; Windows: at login)")
    p.add_argument("--uninstall-service", action="store_true")
    p.add_argument("--version", action="version", version=f"{APP_NAME} agent {APP_VERSION}")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    config_path = (args.config or default_config_path()).expanduser().resolve()

    if args.init_config:
        config = init_config(config_path, token=args.token, port=args.port, name=args.name)
        print(f"config:  {config_path}")
        print(f"port:    {config['port']}")
        print(f"token:   {config['token'] or '(none)'}")
        return 0

    if args.install_service or args.uninstall_service:
        extra = ["--config", str(config_path)]
        ok, message = platform_support.set_autostart(bool(args.install_service), "agent", extra)
        print(message)
        return 0 if ok else 1

    config = load_config(config_path)
    if args.port:
        config["port"] = args.port
    if args.bind:
        config["bind"] = args.bind
    if args.name is not None:
        config["name"] = args.name

    # Qt is imported only now, so --init-config/--install-service work even
    # where PySide6 can't load. QCoreApplication needs no display.
    from PySide6.QtCore import QCoreApplication, QTimer

    from discovery import BeaconSender
    from gpu_stats import get_gpu_reader
    from monitor import OllamaMonitor
    from status_server import StatusServer, build_snapshot

    app = QCoreApplication(sys.argv)
    app.setApplicationName(f"{APP_NAME} agent")

    name = config.get("name") or platform_support.hostname()
    monitor = OllamaMonitor(gpu_reader=get_gpu_reader(),
                            base_url=config.get("ollama_url") or "http://127.0.0.1:11434",
                            pinned=list(config.get("pinned_models") or []))
    server = StatusServer()
    if not server.start(config["bind"], int(config["port"]), config.get("token", "")):
        print(f"error: {server.last_error}", file=sys.stderr)
        return 2
    beacon = BeaconSender()
    if config.get("beacon", True):
        beacon.start(name, int(config["port"]), bool(config.get("token")))

    def publish(rows, gpu):
        server.update(build_snapshot(name, rows, gpu, monitor.base_url,
                                     monitor.ollama_online, monitor.ollama_version))

    monitor.rosterChanged.connect(publish)

    def shutdown(*_):
        app.quit()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)
    # Python only runs signal handlers between bytecodes; while Qt's event loop
    # sits in C++ that never happens, so wake the interpreter periodically.
    wake = QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(250)

    print(f"{APP_NAME} agent {APP_VERSION} - '{name}' serving on "
          f"{config['bind']}:{config['port']} "
          f"({'token required' if config.get('token') else 'NO token'})", flush=True)
    monitor.start()
    code = app.exec()
    monitor.stop()
    beacon.stop()
    server.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
