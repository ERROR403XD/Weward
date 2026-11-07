import http.client
import threading
import unittest
from unittest.mock import patch

from weward.common import make_server
from weward.receiver import ReceiverHandler


class DesktopTest(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {"DESKTOP_PUBLIC_PORT": "6080", "DESKTOP_PUBLIC_URL": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        # No receiver or collector is needed to open the login desktop.
        self.server = make_server(("127.0.0.1", 0), ReceiverHandler, None)
        self.worker = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def request(self, path="/desktop", host="weward.test:18081"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request("GET", path, headers={"Host": host})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_default_desktop_redirect_uses_browser_host_without_collector(self):
        for host in ("weward.test", "100.64.0.1", "[fd7a:115c:a1e0::1]"):
            with self.subTest(host=host):
                status, headers, body = self.request(host=host + ":18081")
                self.assertEqual(status, 302)
                self.assertEqual(headers["Location"], f"http://{host}:6080/vnc.html?autoconnect=1&resize=scale&reconnect=0")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(body, b"")

    def test_custom_published_port(self):
        with patch.dict("os.environ", {"DESKTOP_PUBLIC_PORT": "16080"}):
            self.assertEqual(self.request()[1]["Location"],
                             "http://weward.test:16080/vnc.html?autoconnect=1&resize=scale&reconnect=0")

    def test_full_url_takes_priority_and_preserves_proxy_path_and_query(self):
        url = "https://desktop.test/wechat/vnc.html?path=wechat/websockify&autoconnect=1&resize=scale&reconnect=0"
        with patch.dict("os.environ", {"DESKTOP_PUBLIC_URL": url, "DESKTOP_PUBLIC_PORT": "invalid"}):
            self.assertEqual(self.request()[1]["Location"], url)

    def test_bad_config_only_affects_desktop_entry(self):
        for config in ({"DESKTOP_PUBLIC_URL": "javascript:alert(1)"},
                       {"DESKTOP_PUBLIC_URL": "/vnc.html"},
                       {"DESKTOP_PUBLIC_PORT": "0"},
                       {"DESKTOP_PUBLIC_PORT": "65536"},
                       {"DESKTOP_PUBLIC_PORT": "invalid"}):
            with self.subTest(config=config), patch.dict("os.environ", config):
                self.assertEqual(self.request()[0], 400)
                self.assertEqual(self.request("/healthz")[0], 200)
                self.assertEqual(self.request("/")[0], 200)
