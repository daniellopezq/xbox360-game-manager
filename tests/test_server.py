"""server.py: config loading, the local-network gate, CSRF checks and the API routes (no Xbox needed)."""
import http.client
import ipaddress
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import server  # noqa: E402


class FakeCopier:
    def __init__(self):
        self.sent = []

    def status(self):
        return {"games": [], "queue": list(self.sent)}

    def send(self, gid):
        if gid != "abcdefabcdef":
            return 404, "No such game."
        self.sent.append(gid)
        return 200, None

    def art(self, gid):
        return None


class ConfigTest(unittest.TestCase):
    def write(self, d, cfg):
        p = os.path.join(d, "config.json")
        with open(p, "w") as f:
            json.dump(cfg, f)
        return p

    def test_load(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = server.load_config(self.write(d, {"_comment": "x", "games_dir": d, "allow_clients": ["100.64.0.0/10"]}))
            self.assertEqual(cfg["port"], 8360)
            self.assertEqual(cfg["allow_clients"], [ipaddress.ip_network("100.64.0.0/10")])

    def test_rejects_unknown_keys_and_bad_values(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ({"games_dir": d, "colour": 1}, {"games_dir": d, "port": 0}, {"games_dir": d, "subnet": "x"},
                        {"games_dir": d, "allow_clients": ["nope"]}, {}):
                with self.assertRaises(server.ConfigError, msg=bad):
                    server.load_config(self.write(d, bad))


class LocalClientTest(unittest.TestCase):
    def test_local_networks_only(self):
        for ip in ("127.0.0.1", "192.168.1.20", "10.1.2.3", "172.16.0.9", "::1", "fe80::1", "::ffff:192.168.1.2"):
            self.assertTrue(server.is_local_client(ip), ip)
        for ip in ("8.8.8.8", "100.100.1.1", "2001:4860::1", "junk"):
            self.assertFalse(server.is_local_client(ip), ip)
        self.assertTrue(server.is_local_client("100.100.1.1", [ipaddress.ip_network("100.64.0.0/10")]))


class RoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.copier = FakeCopier()
        cfg = dict(server.DEFAULT_CONFIG, allow_clients=[])
        cls.httpd = server.Server(("127.0.0.1", 0), cfg, cls.copier)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def post(self, path, obj, **headers):
        return self.request("POST", path, json.dumps(obj), dict({"Content-Type": "application/json"}, **headers))

    def test_page_and_static(self):
        r, body = self.request("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"Xbox 360 Game Manager", body)
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))
        for f in ("app.js", "style.css", "icon.svg"):
            self.assertEqual(self.request("GET", "/static/" + f)[0].status, 200, f)
        for bad in ("/static/../server.py", "/static/x.py", "/server.py", "/.env"):
            self.assertEqual(self.request("GET", bad)[0].status, 404, bad)

    def test_status_and_send(self):
        r, body = self.request("GET", "/api/status")
        self.assertEqual(json.loads(body)["games"], [])
        r, body = self.post("/api/send", {"id": "abcdefabcdef"})
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(body)["status"]["queue"], ["abcdefabcdef"])
        r, body = self.post("/api/send", {"id": "000000000000"})
        self.assertEqual((r.status, json.loads(body)["error"]["message"]), (404, "No such game."))

    def test_post_checks(self):
        self.assertEqual(self.post("/api/send", {"id": "x"}, **{"Sec-Fetch-Site": "cross-site"})[0].status, 403)
        self.assertEqual(self.post("/api/send", {"id": "x"}, Origin="http://evil.example")[0].status, 403)
        self.assertEqual(self.request("POST", "/api/send", "{}", {"Content-Type": "text/plain"})[0].status, 415)
        self.assertEqual(self.post("/api/send", {"id": 5})[0].status, 400)
        self.assertEqual(self.post("/api/nope", {})[0].status, 404)
        self.assertEqual(self.request("DELETE", "/api/status")[0].status, 405)

    def test_art_missing(self):
        self.assertEqual(self.request("GET", "/api/art/abcdefabcdef.jpg")[0].status, 404)


if __name__ == "__main__":
    unittest.main()
