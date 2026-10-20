"""Liquid-glass web remote for the Samsung TV — open it on a phone, laptop or the TV's browser.

Run:  python remote.py            (serves on http://<this-computer>:8765)
      python remote.py --port 9000 --host 127.0.0.1
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from tv import SamsungTV

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("samsung-tv.remote")

HERE = Path(__file__).parent
PAGE = HERE / "remote.html"
STATIC = {  # files the page needs for "Add to Home Screen"
    "/manifest.webmanifest": ("remote.webmanifest", "application/manifest+json"),
    "/icon.svg": ("remote-icon.svg", "image/svg+xml"),
    "/icon-180.png": ("remote-icon-180.png", "image/png"),
    "/icon-512.png": ("remote-icon-512.png", "image/png"),
}
KEY_RE = re.compile(r"^[A-Z0-9_]{1,32}$")
MAX_BODY = 4096

_tv = SamsungTV()


def _status() -> dict[str, Any]:
    info = _tv.info()
    if info.get("power") == "on":
        try:
            info["muted"] = _tv.get_mute()
        except Exception:
            info["muted"] = None
    return info


def _text(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{field}' must be a non-empty string")
    return value.strip()


def _launch(body: dict[str, Any]) -> dict[str, Any]:
    app = _text(body, "app")
    _tv.launch_app(app)
    return {"app": app}


def _key(body: dict[str, Any]) -> dict[str, Any]:
    key = _text(body, "key").upper()
    if not KEY_RE.match(key):
        raise ValueError(f"Invalid key '{key}'")
    _tv.send_key(key)
    return {"key": key}


def _volume(body: dict[str, Any]) -> dict[str, Any]:
    if "level" in body:
        level = body["level"]
        if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0 <= level <= 1000:
            raise ValueError("'level' must be a number from 0 to 100")
        level = int(level)
        _tv.set_volume(level)
        return {"volume": max(0, min(100, level))}
    action = str(body.get("action", "")).lower()
    if action in ("up", "down"):
        return {"volume": _tv.step_volume(1 if action == "up" else -1)}
    if action in ("mute", "unmute"):
        _tv.set_mute(action == "mute")
        return {"muted": action == "mute"}
    raise ValueError("Use 'level' (0-100) or action: up, down, mute, unmute")


def _power(body: dict[str, Any]) -> dict[str, Any]:
    action = str(body.get("action", "")).lower()
    if action == "on":
        _tv.power_on()
    elif action == "off":
        _tv.power_off()
    else:
        raise ValueError("Use action 'on' or 'off'")
    return {"power": action}


GET_ROUTES: dict[str, Callable[[dict[str, str]], Any]] = {
    "/api/status": lambda q: _status(),
    "/api/apps": lambda q: {"apps": _tv.installed_apps(refresh=q.get("refresh") == "1")},
    "/api/current": lambda q: {"app": _tv.current_app()},
}
POST_ROUTES: dict[str, Callable[[dict[str, Any]], Any]] = {
    "/api/launch": _launch,
    "/api/key": _key,
    "/api/volume": _volume,
    "/api/power": _power,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "SamsungTVRemote/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict[str, Any]) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def _run(self, fn: Callable[[], Any]) -> None:
        try:
            self._json(200, {"success": True, **fn()})
        except ValueError as e:
            self._json(400, {"success": False, "message": str(e)})
        except Exception as e:
            log.error("%s failed: %s", self.path, e)
            self._json(502, {"success": False, "message": f"TV did not respond: {e}"})

    def do_GET(self) -> None:
        path, _, qs = self.path.partition("?")
        if path in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if path in STATIC:
            name, ctype = STATIC[path]
            self._send(200, (HERE / name).read_bytes(), ctype)
            return
        route = GET_ROUTES.get(path)
        if not route:
            self._json(404, {"success": False, "message": "Not found"})
            return
        query = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        self._run(lambda: route(query))

    def _same_origin(self) -> bool:
        """Block other web pages from driving the TV (CSRF).

        Requiring application/json forces a CORS preflight, which this server
        never answers, and a cross-site Origin is rejected outright.
        """
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.split("://", 1)[-1] == self.headers.get("Host")

    def do_POST(self) -> None:
        route = POST_ROUTES.get(self.path)
        if not route:
            self._json(404, {"success": False, "message": "Not found"})
            return
        if not self._same_origin():
            self._json(403, {"success": False, "message": "Requests must come from this remote's own page"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_BODY:
                raise ValueError
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, RecursionError):
            self._json(400, {"success": False, "message": "Body must be a small JSON object"})
            return
        self._run(lambda: route(body))


def _lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0",
                        help="Interface to listen on (default: all, so phones on your Wi-Fi can connect)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    shown = _lan_ip() if args.host == "0.0.0.0" else args.host
    log.info("TV remote ready at http://%s:%d", shown, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
