"""Offline tests for the TV client and web remote (no TV needed)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tv  # noqa: E402


def _not_found(*_a, **_k):
    raise HTTPError("url", 404, "Not Found", {}, None)


class StateFileCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(tv, "STATE_FILE", Path(self._dir.name) / "tv_state.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._dir.cleanup)


class AppResolutionTests(StateFileCase):
    def test_installed_scan_skips_404_and_dedupes_shared_ids(self):
        installed = {"3201907018807": "Netflix", "3202301029760": "HBO Max"}
        calls = []

        def fake_rest(path, method="GET", timeout=5):
            app_id = path.rsplit("/", 1)[-1]
            calls.append(app_id)
            if app_id in installed:
                return {"id": app_id, "name": installed[app_id]}
            _not_found()

        t = tv.SamsungTV(ip="1.2.3.4")
        with mock.patch.object(t, "_rest", side_effect=fake_rest):
            apps = t.installed_apps()
        self.assertEqual([a["id"] for a in apps], ["3201907018807", "3202301029760"])
        self.assertEqual(len(calls), len(set(calls)), "each app ID is probed once")

    def test_resolve_prefers_installed_candidate(self):
        t = tv.SamsungTV(ip="1.2.3.4")
        with mock.patch.object(t, "installed_apps", return_value=[{"id": "3201907018807"}]):
            self.assertEqual(t._resolve_app_id("netflix"), "3201907018807")
            self.assertEqual(t._resolve_app_id("Netflix"), "3201907018807")

    def test_scan_is_cached_across_instances(self):
        t = tv.SamsungTV(ip="1.2.3.4")
        with mock.patch.object(t, "_rest", return_value={"name": "X"}):
            t.installed_apps()
        t2 = tv.SamsungTV(ip="1.2.3.4")
        with mock.patch.object(t2, "_rest", side_effect=AssertionError("should use cache")):
            self.assertTrue(t2.installed_apps())

    def test_deep_link_goes_over_websocket(self):
        t = tv.SamsungTV(ip="1.2.3.4")
        with mock.patch.object(t, "installed_apps", return_value=[]), \
             mock.patch.object(t, "_rest") as rest, \
             mock.patch.object(t, "_send_ws") as ws:
            t.launch_app("youtube", meta_tag="v=abc")
        rest.assert_not_called()
        ws.assert_called_once_with("run_app", app_id="111299001912", app_type=2, meta_tag="v=abc")

    def test_current_app_prefers_visible(self):
        t = tv.SamsungTV(ip="1.2.3.4")
        apps = [{"id": "a", "alias": "netflix"}, {"id": "b", "alias": "youtube"}]
        status = {"a": {"running": True, "visible": False}, "b": {"running": True, "visible": True}}
        with mock.patch.object(t, "installed_apps", return_value=apps), \
             mock.patch.object(t, "_rest", side_effect=lambda p, **k: status[p.rsplit("/", 1)[-1]]):
            self.assertEqual(t.current_app()["name"], "youtube")


class SoapEscapingTests(StateFileCase):
    def test_media_url_and_title_are_escaped(self):
        t = tv.SamsungTV(ip="1.2.3.4")
        with mock.patch.object(tv, "_soap_call", return_value="") as soap, \
             mock.patch.object(tv.time, "sleep"):
            t.play_media("http://host/v.mp4?a=1&b=2", title="Tom & Jerry <1>")
        body = soap.call_args_list[0].args[4]
        self.assertIn("a=1&amp;b=2", body)
        self.assertNotIn("&b=2", body)
        self.assertIn("Tom &amp;amp; Jerry &amp;lt;1&amp;gt;", body)


class PowerTests(StateFileCase):
    def test_mac_survives_restart(self):
        t = tv.SamsungTV(ip="1.2.3.4")
        with mock.patch.object(t, "_rest", return_value={"device": {"wifiMac": "AA:BB:CC:DD:EE:FF"}}), \
             mock.patch.object(t, "get_volume", return_value=10):
            t.info()
        restarted = tv.SamsungTV(ip="1.2.3.4")
        with mock.patch.object(tv, "wake_on_lan") as wol:
            restarted.power_on()
        wol.assert_called_once_with("AA:BB:CC:DD:EE:FF")

    def test_env_overrides(self):
        with mock.patch.dict("os.environ", {"SAMSUNG_TV_IP": "10.0.0.9", "SAMSUNG_TV_MAC": "11:22:33:44:55:66"}):
            t = tv.SamsungTV()
        self.assertEqual(t._ensure_ip(), "10.0.0.9")
        self.assertEqual(t._mac, "11:22:33:44:55:66")


class ChannelTests(StateFileCase):
    def test_bad_direction_rejected(self):
        t = tv.SamsungTV(ip="1.2.3.4")
        with mock.patch.object(t, "send_key") as key:
            with self.assertRaises(ValueError):
                t.channel(direction="sideways")
        key.assert_not_called()


class RemoteApiTests(StateFileCase):
    def setUp(self):
        super().setUp()
        import remote
        self.remote = remote
        patcher = mock.patch.object(remote, "_tv")
        self.fake = patcher.start()
        self.addCleanup(patcher.stop)

    def test_key_validation(self):
        self.assertEqual(self.remote._key({"key": "volup"}), {"key": "VOLUP"})
        self.fake.send_key.assert_called_once_with("VOLUP")
        for bad in ("", "KEY UP", "../x", "A" * 40):
            with self.assertRaises(ValueError):
                self.remote._key({"key": bad})

    def test_volume_level_is_clamped(self):
        self.assertEqual(self.remote._volume({"level": 150}), {"volume": 100})
        for bad in ({"action": "loud"}, {"level": None}, {"level": [1]}, {"level": "abc"}, {"level": 1e999}):
            with self.assertRaises(ValueError):
                self.remote._volume(bad)
        with self.assertRaises(ValueError):
            self.remote._key({"key": None})

    def test_opens_app_only_when_tv_turns_on(self):
        states = iter([False, False, True, True, False, True])

        class Stop(Exception):
            pass

        def is_on():
            try:
                return next(states)
            except StopIteration:
                raise Stop

        with mock.patch.object(self.remote, "_tv_is_on", side_effect=is_on), \
             mock.patch.object(self.remote, "_open_after_boot") as opener, \
             mock.patch.object(self.remote.time, "sleep"):
            with self.assertRaises(Stop):
                self.remote.watch_power("LqGlassTV1.GlassHome")
        # off->on twice (polls 3 and 6), staying on doesn't relaunch
        self.assertEqual(opener.call_count, 2)

    def test_open_after_boot_retries_until_visible(self):
        responses = iter([OSError("booting"), True, {"visible": False}, True, {"visible": True}])

        def rest(path, method="GET", timeout=5):
            r = next(responses)
            if isinstance(r, Exception):
                raise r
            return r

        self.fake._rest.side_effect = rest
        with mock.patch.object(self.remote.time, "sleep"):
            self.assertTrue(self.remote._open_after_boot("LqGlassTV1.GlassHome"))

    def _server(self):
        import threading
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self.remote.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _post(self, port, body, headers):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/power", body=body, headers=headers)
        r = c.getresponse()
        return r.status

    def test_cross_site_posts_rejected(self):
        port = self._server()
        host = f"127.0.0.1:{port}"
        # text/plain "simple" request from another site: no preflight, must be refused
        self.assertEqual(self._post(port, '{"action":"off"}', {"Content-Type": "text/plain", "Origin": "http://evil.com"}), 403)
        self.assertEqual(self._post(port, '{"action":"off"}', {"Content-Type": "application/json", "Origin": "http://evil.com"}), 403)
        self.fake.power_off.assert_not_called()
        self.assertEqual(self._post(port, '{"action":"off"}', {"Content-Type": "application/json", "Origin": f"http://{host}"}), 200)
        self.fake.power_off.assert_called_once()

    def test_oversized_and_nested_bodies_rejected(self):
        port = self._server()
        json_hdr = {"Content-Type": "application/json"}
        self.assertEqual(self._post(port, "[" * 100000 + "]" * 100000, json_hdr), 400)
        self.assertEqual(self._post(port, '{"level": null}', json_hdr), 400)


class HttpErrorTests(StateFileCase):
    def test_server_errors_are_not_cached_as_uninstalled(self):
        t = tv.SamsungTV(ip="1.2.3.4")

        def flaky(path, method="GET", timeout=5):
            raise HTTPError("url", 503, "Busy", {}, None)

        with mock.patch.object(t, "_rest", side_effect=flaky):
            with self.assertRaises(HTTPError):
                t.installed_apps()
        self.assertIsNone(t._installed)


if __name__ == "__main__":
    unittest.main()
