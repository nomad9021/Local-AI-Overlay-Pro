"""
Finding other machines on the LAN, so adding a host doesn't start with "go
look up its IP address".

Two complementary mechanisms, both driven from Settings -> Hosts -> Discover:

1. Beacons. Every agent (and every overlay with sharing on) broadcasts a small
   UDP datagram every few seconds announcing its name, port and whether it
   wants a token. Listening for a few seconds collects them. This finds agents
   on any port, but only on the local broadcast domain.
2. A subnet sweep. A TCP connect to the Ollama and agent ports across this
   machine's /24, followed by a real HTTP check on anything that answers. This
   finds bare Ollama servers (OLLAMA_HOST=0.0.0.0) that don't run an agent. It
   only runs when the user clicks Discover, and only over the local /24.

Neither path ever sends a token anywhere; tokens are typed in by the user.
"""
import ipaddress
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests

from error_log import log_exception
from platform_support import APP_ID, hostname

BEACON_PORT = 11481
BEACON_INTERVAL_S = 5.0
DEFAULT_OLLAMA_PORT = 11434
DEFAULT_AGENT_PORT = 11480


def local_ipv4() -> Optional[str]:
    """This machine's primary LAN address (no packets are actually sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return None
    finally:
        sock.close()


class BeaconSender:
    """Background UDP broadcaster announcing this machine's status server."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._payload = b""

    def start(self, name: str, port: int, token_required: bool) -> None:
        self._payload = json.dumps({
            "app": APP_ID, "name": name or hostname(), "port": int(port),
            "token_required": bool(token_required),
        }).encode("utf-8")
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="beacon", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            while not self._stop.is_set():
                try:
                    sock.sendto(self._payload, ("255.255.255.255", BEACON_PORT))
                except OSError:
                    pass  # no network right now; try again next interval
                self._stop.wait(BEACON_INTERVAL_S)
        finally:
            sock.close()


def listen_for_beacons(seconds: float = 6.0) -> list:
    """Collect agent beacons for `seconds`. Returns [{name, address, port, ...}]."""
    found = {}
    own = {local_ipv4(), "127.0.0.1"}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", BEACON_PORT))
        sock.settimeout(0.5)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                data, (ip, _port) = sock.recvfrom(2048)
            except socket.timeout:
                continue
            try:
                beacon = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if beacon.get("app") != APP_ID or ip in own:
                continue
            found[(ip, beacon.get("port"))] = {
                "name": str(beacon.get("name") or ip),
                "address": f"{ip}:{int(beacon.get('port') or DEFAULT_AGENT_PORT)}",
                "mode": "agent",
                "token_required": bool(beacon.get("token_required")),
                "via": "beacon",
            }
    except OSError:
        log_exception("listen_for_beacons")
    finally:
        sock.close()
    return list(found.values())


def _probe(ip: str, port: int, timeout: float) -> Optional[dict]:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            pass
    except OSError:
        return None
    try:
        if port == DEFAULT_OLLAMA_PORT:
            resp = requests.get(f"http://{ip}:{port}/api/version", timeout=1.5)
            version = resp.json().get("version")
            if not version:
                return None
            return {"name": _reverse_name(ip), "address": f"{ip}:{port}", "mode": "ollama",
                    "token_required": False, "via": f"Ollama {version}"}
        resp = requests.get(f"http://{ip}:{port}/health", timeout=1.5)
        body = resp.json()
        if body.get("agent") != APP_ID:
            return None
        return {"name": _reverse_name(ip), "address": f"{ip}:{port}", "mode": "agent",
                "token_required": bool(body.get("token_required")), "via": "agent"}
    except Exception:
        return None


def _reverse_name(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0].split(".")[0]
    except Exception:
        return ip


def scan_subnet(timeout: float = 0.35) -> list:
    """Sweep this machine's /24 for Ollama servers and agents."""
    ip = local_ipv4()
    if not ip or ip.startswith("127."):
        return []
    network = ipaddress.ip_network(f"{ip}/24", strict=False)
    targets = [(str(h), port) for h in network.hosts() if str(h) != ip
               for port in (DEFAULT_OLLAMA_PORT, DEFAULT_AGENT_PORT)]
    with ThreadPoolExecutor(max_workers=96) as pool:
        results = pool.map(lambda t: _probe(t[0], t[1], timeout), targets)
    return [r for r in results if r]


def discover(seconds: float = 6.0) -> list:
    """Beacons and a subnet sweep in parallel, de-duplicated by address."""
    beacon_result, scan_result = [], []

    def beacons():
        beacon_result.extend(listen_for_beacons(seconds))

    thread = threading.Thread(target=beacons, daemon=True)
    thread.start()
    try:
        scan_result.extend(scan_subnet())
    except Exception:
        log_exception("discover scan_subnet")
    thread.join(seconds + 2)

    merged = {}
    for entry in scan_result + beacon_result:   # beacons win: they carry the real name
        merged[entry["address"]] = entry
    return sorted(merged.values(), key=lambda e: e["address"])
