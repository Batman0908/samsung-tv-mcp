"""Samsung Smart TV client — WebSocket + UPnP + SSDP + WoL."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from xml.sax.saxutils import escape

from samsungtvws import SamsungTVWS

log = logging.getLogger("samsung-tv")

WS_TIMEOUT = 5
# TU7000's secure WebSocket handshake takes ~7s; a shorter timeout falls back
# to the unpaired port 8001, which the TV rejects as unauthorized.
WS_OPEN_TIMEOUT = 15
TOKEN_FILE = str(Path(__file__).parent / "token.json")
STATE_FILE = Path(__file__).parent / "tv_state.json"
INSTALLED_TTL = 24 * 3600  # installed apps rarely change; tv_apps(refresh=True) rescans
# The TV's REST server times out under concurrent requests, so every
# network call to the TV goes through this lock (re-entrant for nesting).
_IO_LOCK = threading.RLock()
_UPNP_NS = "urn:schemas-upnp-org:service"
_SOAP_ENV = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>{body}</s:Body></s:Envelope>"
)

APP_ALIASES: dict[str, list[str]] = {
    "netflix": ["11101200001", "3201907018807"],
    "youtube": ["111299001912"],
    "prime": ["3201512006785", "3201910019365"],
    "disney": ["3201901017640"],
    "spotify": ["3201606009684"],
    "apple tv": ["3201807016597"],
    "hbo": ["3201601007230", "3202301029760"],
    "max": ["3202301029760", "3201601007230"],
    "plex": ["3201512006963"],
    "browser": ["org.tizen.browser", "3201907018784"],
    "steam link": ["3201702011851"],
    "twitch": ["3202203026841"],
    "tiktok": ["3202008021577"],
    "tubi": ["3201504001965"],
    "pluto": ["3201808016802"],
    "paramount": ["3201710014981"],
    "gallery": ["3201710015037"],
    "smartthings": ["3201710015016"],
}

APPS_FILE = Path(__file__).parent / "apps.json"


def _load_custom_apps() -> None:
    """Merge user-defined apps from apps.json, e.g. {"ibo player": ["3202..."]}."""
    try:
        custom = json.loads(APPS_FILE.read_text())
    except OSError:
        return
    except ValueError as e:
        log.warning("Ignoring %s: %s", APPS_FILE, e)
        return
    for name, ids in custom.items():
        ids = [ids] if isinstance(ids, str) else ids
        if isinstance(ids, list) and all(isinstance(i, str) for i in ids):
            APP_ALIASES[name.lower().strip()] = ids


_load_custom_apps()


NAVIGATE_KEYS = {
    "home": "KEY_HOME", "back": "KEY_RETURN", "exit": "KEY_EXIT",
    "menu": "KEY_MENU", "source": "KEY_SOURCE", "guide": "KEY_GUIDE",
    "info": "KEY_INFO", "tools": "KEY_TOOLS",
    "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
    "enter": "KEY_ENTER", "ok": "KEY_ENTER",
    "play": "KEY_PLAY", "pause": "KEY_PAUSE", "stop": "KEY_STOP",
    "ff": "KEY_FF", "rewind": "KEY_REWIND",
}


# ── SSDP Discovery ──────────────────────────────────────────────


def discover(timeout: float = 4.0) -> list[dict[str, Any]]:
    """Discover Samsung TVs on the local network via SSDP."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {int(timeout)}\r\n"
        "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n"
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.sendto(msg.encode(), ("239.255.255.250", 1900))

    locations: set[str] = set()
    tvs: list[dict[str, Any]] = []
    try:
        while True:
            data, _ = sock.recvfrom(4096)
            text = data.decode(errors="ignore")
            if "samsung" not in text.lower():
                continue
            for line in text.splitlines():
                if line.upper().startswith("LOCATION:"):
                    locations.add(line.split(":", 1)[1].strip())
    except socket.timeout:
        pass
    finally:
        sock.close()

    for loc in locations:
        try:
            resp = urlopen(loc, timeout=3).read().decode()
            root = ET.fromstring(resp)
            ns = {"d": "urn:schemas-upnp-org:device-1-0"}
            dev = root.find(".//d:device", ns)
            if dev is None:
                continue
            fn = dev.findtext("d:friendlyName", "", ns)
            mn = dev.findtext("d:modelName", "", ns)
            mfr = dev.findtext("d:manufacturer", "", ns)
            if "samsung" not in (mfr or "").lower():
                continue
            tvs.append({
                "ip": urlparse(loc).hostname,
                "name": fn, "model": mn, "manufacturer": mfr,
            })
        except Exception:
            continue
    return tvs


# ── UPnP SOAP ───────────────────────────────────────────────────


def _soap_call(
    ip: str, control_url: str, service: str, action: str, args: str = ""
) -> str:
    body = f'<u:{action} xmlns:u="{_UPNP_NS}:{service}:1">{args}</u:{action}>'
    envelope = _SOAP_ENV.format(body=body)
    req = Request(
        f"http://{ip}:9197{control_url}",
        data=envelope.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{_UPNP_NS}:{service}:1#{action}"',
        },
    )
    with _IO_LOCK:
        return urlopen(req, timeout=WS_TIMEOUT).read().decode()


def _soap_value(xml_text: str, tag: str) -> str | None:
    root = ET.fromstring(xml_text)
    for elem in root.iter():
        if elem.tag.endswith(tag):
            return elem.text
    return None


# ── Wake-on-LAN ─────────────────────────────────────────────────


def wake_on_lan(mac: str) -> None:
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # A few packets: TVs on Wi-Fi standby often miss the first one
        for _ in range(3):
            s.sendto(packet, ("255.255.255.255", 9))
            time.sleep(0.1)


# ── Persisted state (MAC survives restarts so power-on works) ───


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(**values: Any) -> None:
    state = _load_state()
    if all(state.get(k) == v for k, v in values.items()):
        return
    state.update(values)
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as e:
        log.warning("Could not save %s: %s", STATE_FILE, e)


# ── Main TV Client ──────────────────────────────────────────────


class SamsungTV:
    """Unified Samsung TV controller — WebSocket, REST, UPnP."""

    def __init__(self, ip: str | None = None, mac: str | None = None):
        state = _load_state()
        self._ip = ip or os.environ.get("SAMSUNG_TV_IP") or None
        # A MAC given explicitly (e.g. the wired MAC) wins over the Wi-Fi MAC the TV reports
        self._mac_pinned = mac or os.environ.get("SAMSUNG_TV_MAC") or None
        self._mac = self._mac_pinned or state.get("mac")
        self._ws: SamsungTVWS | None = None
        self._installed: list[dict[str, Any]] | None = None
        self._installed_at = 0.0

    # ── Connection ───────────────────────────────────────────

    def _ensure_ip(self) -> str:
        if self._ip:
            return self._ip
        with _IO_LOCK:
            if self._ip:
                return self._ip
            tvs = discover(timeout=4.0)
            if not tvs:
                raise ConnectionError(
                    "No Samsung TV found on the network. "
                    "Set SAMSUNG_TV_IP if discovery is blocked."
                )
            self._ip = tvs[0]["ip"]
            log.info("Auto-discovered TV at %s (%s)", self._ip, tvs[0].get("name"))
            return self._ip

    def _rest(self, path: str, method: str = "GET", timeout: float = WS_TIMEOUT) -> Any:
        ip = self._ensure_ip()
        req = Request(f"http://{ip}:8001/api/v2/{path}", method=method)
        with _IO_LOCK:
            raw = urlopen(req, timeout=timeout).read()
        try:
            return json.loads(raw) if raw.strip() else None
        except ValueError:
            if method != "GET":  # the action already happened; body is informational
                return None
            raise

    def _ensure_ws(self) -> SamsungTVWS:
        if self._ws is not None:
            return self._ws
        ip = self._ensure_ip()
        # First pairing waits for someone to press "Allow" on the TV
        timeout = WS_OPEN_TIMEOUT if Path(TOKEN_FILE).exists() else 30
        try:
            self._ws = SamsungTVWS(
                host=ip, port=8002, token_file=TOKEN_FILE,
                timeout=timeout, name="ClaudeCode", key_press_delay=0,
            )
            self._ws.open()
            log.info("Connected via WSS:8002")
        except Exception:
            log.warning("WSS:8002 failed, trying WS:8001")
            self._ws = SamsungTVWS(
                host=ip, port=8001, token_file=TOKEN_FILE,
                timeout=timeout, name="ClaudeCode", key_press_delay=0,
            )
            self._ws.open()
        return self._ws

    def _send_ws(self, method: str, **kwargs: Any) -> Any:
        """Send WebSocket command with auto-reconnect."""
        with _IO_LOCK:
            for attempt in range(2):
                ws = self._ensure_ws()
                try:
                    return getattr(ws, method)(**kwargs)
                except Exception as e:
                    self._ws = None
                    if attempt == 0:
                        log.warning("WS error (%s), reconnecting...", e)
                    else:
                        raise ConnectionError(f"TV WebSocket failed after retry: {e}") from e

    # ── Device Info ──────────────────────────────────────────

    def info(self) -> dict[str, Any]:
        ip = self._ensure_ip()
        try:
            data = self._rest("")
            device = data.get("device", {})
            if device.get("wifiMac") and not self._mac_pinned:
                self._mac = device["wifiMac"]
                _save_state(mac=self._mac)
            try:
                device["currentVolume"] = self.get_volume()
            except Exception:
                pass
            return {
                "name": device.get("name", "Unknown"),
                "model": device.get("modelName", "Unknown"),
                "ip": device.get("ip", ip),
                "mac": self._mac,
                "power": device.get("PowerState", "unknown"),
                "os": device.get("OS", "Tizen"),
                "resolution": device.get("resolution", "unknown"),
                "network": device.get("networkType", "unknown"),
                "volume": device.get("currentVolume"),
            }
        except (URLError, OSError, ValueError):
            return {"power": "off", "ip": ip, "note": "TV appears to be off"}

    # ── Power ────────────────────────────────────────────────

    def power_off(self) -> None:
        self._send_ws("send_key", key="KEY_POWER")

    def power_on(self, mac: str | None = None) -> None:
        target_mac = mac or self._mac
        if not target_mac:
            try:
                self.info()
                target_mac = self._mac
            except Exception:
                pass
        if not target_mac:
            raise ValueError(
                "TV MAC address unknown. Run tv_info once while the TV is on "
                "(the MAC is saved for next time), or set SAMSUNG_TV_MAC."
            )
        wake_on_lan(target_mac)

    # ── Keys ─────────────────────────────────────────────────

    def send_key(self, key: str, times: int = 1) -> None:
        normalized = key.upper()
        if not normalized.startswith("KEY_"):
            normalized = f"KEY_{normalized}"
        for i in range(times):
            self._send_ws("send_key", key=normalized)
            if i < times - 1:
                time.sleep(0.15)

    def send_keys(self, keys: list[str], delay: float = 0.3) -> None:
        for i, key in enumerate(keys):
            self.send_key(key)
            if i < len(keys) - 1:
                time.sleep(delay)

    def navigate(self, action: str) -> None:
        key = NAVIGATE_KEYS.get(action.lower())
        if not key:
            raise ValueError(f"Unknown: '{action}'. Valid: {', '.join(NAVIGATE_KEYS)}")
        self.send_key(key)

    # ── Volume ───────────────────────────────────────────────

    def get_volume(self) -> int:
        ip = self._ensure_ip()
        xml = _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "GetVolume", "<InstanceID>0</InstanceID><Channel>Master</Channel>",
        )
        val = _soap_value(xml, "CurrentVolume")
        return int(val) if val else -1

    def set_volume(self, level: int) -> None:
        ip = self._ensure_ip()
        _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "SetVolume",
            f"<InstanceID>0</InstanceID><Channel>Master</Channel>"
            f"<DesiredVolume>{max(0, min(100, level))}</DesiredVolume>",
        )

    def step_volume(self, delta: int) -> int:
        """Change volume by delta via UPnP (the VOLUP/VOLDOWN keys are sometimes dropped)."""
        with _IO_LOCK:
            level = max(0, min(100, self.get_volume() + delta))
            self.set_volume(level)
        return level

    def get_mute(self) -> bool:
        ip = self._ensure_ip()
        xml = _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "GetMute", "<InstanceID>0</InstanceID><Channel>Master</Channel>",
        )
        return _soap_value(xml, "CurrentMute") == "1"

    def set_mute(self, mute: bool) -> None:
        ip = self._ensure_ip()
        _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "SetMute",
            f"<InstanceID>0</InstanceID><Channel>Master</Channel>"
            f"<DesiredMute>{'1' if mute else '0'}</DesiredMute>",
        )

    # ── Channel ──────────────────────────────────────────────

    def channel(self, number: int | None = None, direction: str | None = None) -> None:
        if number is not None:
            for digit in str(number):
                self.send_key(f"KEY_{digit}")
                time.sleep(0.15)
            time.sleep(0.3)
            self.send_key("KEY_ENTER")
        elif direction:
            d = direction.lower()
            if d not in ("up", "down"):
                raise ValueError(f"Unknown direction '{direction}'. Use 'up' or 'down'")
            self.send_key("KEY_CHUP" if d == "up" else "KEY_CHDOWN")
        else:
            raise ValueError("Provide either number or direction ('up'/'down')")

    # ── Apps ─────────────────────────────────────────────────

    def _candidate_ids(self, name_or_id: str) -> list[str]:
        alias = name_or_id.lower().strip()
        if alias in APP_ALIASES:
            return APP_ALIASES[alias]
        if name_or_id.replace(".", "").replace("_", "").isalnum() and (
            len(name_or_id) > 8 or "." in name_or_id
        ):
            return [name_or_id]
        for key, ids in APP_ALIASES.items():
            if alias in key or key in alias:
                return ids
        return [name_or_id]

    def _resolve_app_id(self, name_or_id: str) -> str:
        """Pick the candidate ID that is actually installed on this TV.

        Apps ship under different IDs per model year (e.g. Netflix is
        3201907018807 on 2020 TVs, not 11101200001), so the first alias
        ID is often wrong.
        """
        ids = self._candidate_ids(name_or_id)
        if len(ids) > 1:
            installed = {a["id"] for a in self.installed_apps()}
            for app_id in ids:
                if app_id in installed:
                    return app_id
        return ids[0]

    def installed_apps(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Known apps that are installed on this TV, probed via REST.

        The TV has no reliable "list apps" call on 2020 models, so each alias
        ID is probed (404 = not installed). Probing takes ~1.5s per ID, so
        the result is cached in memory and in tv_state.json.
        """
        now = time.time()
        known = sorted({i for ids in APP_ALIASES.values() for i in ids})
        if not refresh and self._installed is None:
            cached = _load_state().get("installed")
            # Rescan if apps.json added or removed apps since the last scan
            if cached and now - cached.get("at", 0) < INSTALLED_TTL and cached.get("known") == known:
                self._installed, self._installed_at = cached["apps"], cached["at"]
        if not refresh and self._installed is not None and now - self._installed_at < INSTALLED_TTL:
            return self._installed

        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for alias, ids in APP_ALIASES.items():
            for app_id in ids:
                if app_id in seen:  # hbo/max share IDs
                    continue
                seen.add(app_id)
                try:
                    data = self._rest(f"applications/{app_id}")
                except HTTPError as e:
                    if e.code != 404:  # don't cache a TV hiccup as "not installed"
                        raise
                    continue
                found.append({
                    "id": app_id,
                    "name": data.get("name") or alias.title(),
                    "alias": alias,
                })
                break
        self._installed, self._installed_at = found, now
        _save_state(installed={"at": now, "apps": found, "known": known})
        return found

    def list_apps(self) -> list[dict[str, Any]]:
        """Return known apps installed on this TV (direct query unsupported on TU7000)."""
        return self.installed_apps()

    def launch_app(self, name_or_id: str, meta_tag: str | None = None) -> None:
        app_id = self._resolve_app_id(name_or_id)
        # REST launch can't carry a deep link, so deep links go over WebSocket
        if not meta_tag:
            try:
                self._rest(f"applications/{app_id}", method="POST")
                return
            except Exception as e:
                log.info("REST launch of %s failed (%s), trying WebSocket", app_id, e)
        self._send_ws("run_app", app_id=app_id, app_type=2, meta_tag=meta_tag or "")

    def close_app(self, name_or_id: str) -> None:
        app_id = self._resolve_app_id(name_or_id)
        self._rest(f"applications/{app_id}", method="DELETE")

    # ── Browser ──────────────────────────────────────────────

    def open_browser(self, url: str) -> None:
        self._send_ws("open_browser", url=url)

    # ── Text & Cursor ────────────────────────────────────────

    def send_text(self, text: str) -> None:
        self._send_ws("send_text", text=text)

    def move_cursor(self, x: int, y: int, duration: int = 500) -> None:
        self._send_ws("move_cursor", x=x, y=y, duration=duration)

    # ── DLNA Media ───────────────────────────────────────────

    def play_media(self, url: str, title: str = "Media") -> None:
        ip = self._ensure_ip()
        meta = (
            f'<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
            f' xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<item><dc:title>{escape(title)}</dc:title></item></DIDL-Lite>"
        )
        _soap_call(
            ip, "/upnp/control/AVTransport1", "AVTransport",
            "SetAVTransportURI",
            f"<InstanceID>0</InstanceID>"
            f"<CurrentURI>{escape(url)}</CurrentURI>"
            # DIDL-Lite is itself XML carried as text, so escape it once more
            f"<CurrentURIMetaData>{escape(meta)}</CurrentURIMetaData>",
        )
        time.sleep(0.5)
        _soap_call(
            ip, "/upnp/control/AVTransport1", "AVTransport",
            "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>",
        )

    def media_control(self, action: str, target: str | None = None) -> dict[str, Any]:
        ip = self._ensure_ip()
        a = action.lower()
        if a == "play":
            _soap_call(ip, "/upnp/control/AVTransport1", "AVTransport",
                       "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
        elif a == "pause":
            _soap_call(ip, "/upnp/control/AVTransport1", "AVTransport",
                       "Pause", "<InstanceID>0</InstanceID>")
        elif a == "stop":
            _soap_call(ip, "/upnp/control/AVTransport1", "AVTransport",
                       "Stop", "<InstanceID>0</InstanceID>")
        elif a == "seek" and target:
            _soap_call(ip, "/upnp/control/AVTransport1", "AVTransport",
                       "Seek", f"<InstanceID>0</InstanceID>"
                       f"<Unit>REL_TIME</Unit><Target>{escape(target)}</Target>")
        elif a == "status":
            xml_t = _soap_call(ip, "/upnp/control/AVTransport1", "AVTransport",
                               "GetTransportInfo", "<InstanceID>0</InstanceID>")
            xml_p = _soap_call(ip, "/upnp/control/AVTransport1", "AVTransport",
                               "GetPositionInfo", "<InstanceID>0</InstanceID>")
            return {
                "state": _soap_value(xml_t, "CurrentTransportState"),
                "position": _soap_value(xml_p, "RelTime"),
                "duration": _soap_value(xml_p, "TrackDuration"),
                "uri": _soap_value(xml_p, "TrackURI"),
            }
        else:
            raise ValueError(f"Unknown: '{action}'. Valid: play, pause, stop, seek, status")
        return {"action": a, "done": True}

    # ── App Detection ────────────────────────────────────────

    def current_app(self) -> dict[str, Any] | None:
        """Detect which known app is running, preferring the visible one."""
        running = None
        for app in self.installed_apps():
            try:
                data = self._rest(f"applications/{app['id']}", timeout=3)
            except HTTPError as e:
                if e.code != 404:
                    raise
                continue
            if not data.get("running"):
                continue
            hit = {"name": app["alias"], "id": app["id"], "visible": bool(data.get("visible"))}
            if hit["visible"]:
                return hit
            running = running or hit
        return running

    # ── Aspect Ratio ──────────────────────────────────────────

    def get_aspect_ratio(self) -> str:
        ip = self._ensure_ip()
        xml = _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "X_GetAspectRatio", "<InstanceID>0</InstanceID>",
        )
        return _soap_value(xml, "AspectRatio") or "Unknown"

    def set_aspect_ratio(self, ratio: str) -> None:
        ip = self._ensure_ip()
        _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "X_SetAspectRatio",
            f"<InstanceID>0</InstanceID><AspectRatio>{escape(ratio)}</AspectRatio>",
        )

    # ── Captions ──────────────────────────────────────────────

    def get_captions(self) -> dict[str, str]:
        ip = self._ensure_ip()
        xml = _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "X_GetCaptionState", "<InstanceID>0</InstanceID>",
        )
        return {
            "captions": _soap_value(xml, "Captions") or "",
            "enabled": _soap_value(xml, "EnabledCaptions") or "",
        }

    def close(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
