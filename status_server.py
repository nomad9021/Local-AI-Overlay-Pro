"""
The passthrough: a tiny read-only HTTP server that publishes this machine's
Ollama status so overlays on *other* machines can show it.

Why not just point remote overlays at Ollama's own API? They can (that's the
"Ollama API" host mode in remote.py), but /api/ps alone can't say which model is
generating, has no tok/s, and knows nothing about the GPU. Everything that
makes the local HUD useful — /slots busy state, per-runner speed, NVML — only
works on the machine running Ollama. So that machine runs the same
OllamaMonitor it would for its own HUD and serves the result:

    GET /api/v1/status   -> full snapshot (models, states, tok/s, GPU, version)
    GET /health          -> {"ok": true}   (no token required)

It runs inside the GUI app when Settings -> Sharing is on, or standalone on a
headless server via agent.py. It is strictly read-only: nothing here can load,
unload or stop anything, so the worst an unauthorised viewer learns is which
models are loaded. Set a token anyway on anything reachable beyond your LAN —
clients send it as `Authorization: Bearer <token>`.

The HTTP server runs on its own threads; the UI/monitor thread only swaps in a
new snapshot dict under a lock, so a slow client can never stall the HUD.
"""
import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from error_log import log_exception
from platform_support import APP_ID, APP_VERSION, hostname

API_PATH = "/api/v1/status"


def generate_token() -> str:
    return secrets.token_urlsafe(18)


def build_snapshot(name: str, rows: list, gpu, ollama_url: str,
                   ollama_online: bool, ollama_version: str) -> dict:
    import platform as _platform
    return {
        "agent": APP_ID,
        "agent_version": APP_VERSION,
        "name": name or hostname(),
        "hostname": hostname(),
        "platform": f"{_platform.system()} {_platform.release()}".strip(),
        "timestamp": time.time(),
        "ollama": {
            "url": ollama_url,
            "online": bool(ollama_online),
            "version": ollama_version or "",
        },
        "models": [
            {
                "name": s.name,
                "state": s.state.value,
                "size_vram_gb": round(float(s.size_vram_gb or 0.0), 3),
                "seconds_left": s.seconds_left,
                "tok_s": s.tok_s,
                "source": s.source,
                "pinned": bool(s.pinned),
            }
            for s in rows
        ],
        "gpu": {
            "available": bool(getattr(gpu, "available", False)),
            "name": getattr(gpu, "name", "GPU"),
            "gpu_pct": float(getattr(gpu, "gpu_pct", 0.0) or 0.0),
            "vram_used_gb": float(getattr(gpu, "vram_used_gb", 0.0) or 0.0),
            "vram_total_gb": float(getattr(gpu, "vram_total_gb", 0.0) or 0.0),
        },
    }


class StatusServer:
    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot: dict = {"agent": APP_ID, "models": [], "starting": True}
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._token = ""
        self.last_error = ""
        self.config = None   # (bind, port, token) currently serving

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def update(self, snapshot: dict) -> None:
        with self._lock:
            self._snapshot = snapshot

    def _current(self) -> dict:
        with self._lock:
            return self._snapshot

    def start(self, bind: str, port: int, token: str = "") -> bool:
        if self.running and self.config == (bind, int(port), token):
            return True
        self.stop()
        self._token = token or ""
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"{APP_ID}/{APP_VERSION}"

            def log_message(self, *_args):  # silence per-request stderr noise
                pass

            def _send(self, code: int, body: dict) -> None:
                payload = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def _authorised(self, query: dict) -> bool:
                expected = server_ref._token
                if not expected:
                    return True
                supplied = ""
                header = self.headers.get("Authorization", "")
                if header.lower().startswith("bearer "):
                    supplied = header[7:].strip()
                elif query.get("token"):
                    supplied = query["token"][0]
                return hmac.compare_digest(supplied.encode(), expected.encode())

            def do_GET(self):  # noqa: N802 — http.server naming
                try:
                    parts = urlsplit(self.path)
                    if parts.path == "/health":
                        self._send(200, {"ok": True, "agent": APP_ID,
                                         "token_required": bool(server_ref._token)})
                        return
                    if parts.path != API_PATH:
                        self._send(404, {"error": "not found"})
                        return
                    if not self._authorised(parse_qs(parts.query)):
                        self._send(401, {"error": "invalid or missing token"})
                        return
                    self._send(200, server_ref._current())
                except Exception:
                    log_exception("StatusServer.do_GET")

        try:
            httpd = ThreadingHTTPServer((bind or "0.0.0.0", int(port)), Handler)
        except OSError as exc:
            self.last_error = f"Couldn't listen on {bind}:{port} — {exc.strerror or exc}"
            return False
        httpd.daemon_threads = True
        self._httpd = httpd
        self.config = (bind, int(port), token)
        self.last_error = ""
        self._thread = threading.Thread(target=httpd.serve_forever, name="status-server",
                                        kwargs={"poll_interval": 0.5}, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        self.config = None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                log_exception("StatusServer.stop")
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
