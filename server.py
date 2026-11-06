#!/usr/bin/env python3
"""Xbox 360 Game Manager: a small web page to copy games to an Xbox 360 running Aurora, from a phone or PC.

Serves web/ and a JSON API on the local network. The work (finding the Xbox, copying, reading drives) is in
xbox.py. Python 3 standard library only. See README.md.
"""
import argparse
import ipaddress
import json
import logging
import os
import re
import signal
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import xbox

VERSION = "0.1.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
DEFAULT_CONFIG = dict(xbox.DEFAULT_XBOX_CONFIG, bind="0.0.0.0", port=8360, allow_clients=[])

log = logging.getLogger("xboxmgr")


class ConfigError(Exception):
    pass


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
        except (OSError, ValueError) as exc:
            raise ConfigError("%s: %s" % (path, exc))
        if not isinstance(user, dict):
            raise ConfigError("config must be a JSON object")
        for key, value in user.items():
            if key.startswith("_"):
                continue
            if key not in cfg:
                raise ConfigError("unknown config key: %s" % key)
            cfg[key] = value
    if not isinstance(cfg["port"], int) or not 1 <= cfg["port"] <= 65535:
        raise ConfigError("port must be 1-65535")
    try:
        cfg["allow_clients"] = [ipaddress.ip_network(n) for n in cfg["allow_clients"]]
    except (TypeError, ValueError):
        raise ConfigError("allow_clients must be a list of networks, like [\"100.64.0.0/10\"]")
    try:
        xbox.validate_xbox_config(cfg)
    except (ValueError, KeyError, TypeError) as exc:
        raise ConfigError(str(exc))
    return cfg


def is_local_client(ip, extra=()):
    """Loopback, private (RFC 1918 / ULA) and link-local addresses, plus the configured allow_clients."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return addr.is_loopback or addr.is_private or addr.is_link_local or any(addr in n for n in extra)


class Handler(BaseHTTPRequestHandler):
    server_version = "XboxGameManager/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 20

    @property
    def copier(self):
        return self.server.copier

    # --- responses ---
    def send(self, status, body, ctype, head=False, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def send_json(self, obj, status=200, head=False):
        self.send(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json; charset=utf-8",
                  head, {"Cache-Control": "no-store"})

    def send_error_json(self, status, message):
        self.close_connection = True
        self.send_json({"ok": False, "error": {"message": message}}, status)

    def allowed(self):
        if is_local_client(self.client_address[0], self.server.cfg["allow_clients"]):
            return True
        self.send_error_json(403, "Only available on the local network.")
        return False

    # --- GET ---
    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        if not self.allowed():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            return self.static("index.html", head)
        if path == "/api/status":
            return self.send_json(self.copier.status(), head=head)
        m = re.match(r"^/api/art/([0-9a-f]{12})\.jpg$", path)
        if m:
            full = self.copier.art(m.group(1))
            if not full:
                return self.send_json({"error": "not_found"}, 404, head)
            with open(full, "rb") as f:
                return self.send(200, f.read(), "image/jpeg", head, {"Cache-Control": "private, max-age=86400"})
        m = re.match(r"^/static/([a-z0-9-]+\.(?:js|css|svg))$", path)
        if m:
            return self.static(m.group(1), head)
        self.send(404, b"Not found\n", "text/plain; charset=utf-8", head)

    def static(self, name, head):
        try:
            with open(os.path.join(WEB_DIR, name), "rb") as f:
                body = f.read()
        except OSError:
            return self.send(404, b"Not found\n", "text/plain; charset=utf-8", head)
        self.send(200, body, STATIC_TYPES[os.path.splitext(name)[1]], head, {"Cache-Control": "no-cache"})

    # --- POST ---
    ACTIONS = {
        "/api/send": lambda c, b: c.send(b.get("id")),
        "/api/cancel": lambda c, b: c.cancel(b.get("id")),
        "/api/play": lambda c, b: c.play(b.get("id")),
        "/api/rescan": lambda c, b: (c.rescan(), (200, None))[1],
        "/api/name-drive": lambda c, b: c.name_drive(),
        "/api/drive-size": lambda c, b: c.set_capacity(b.get("gb")),
    }

    def do_POST(self):
        if not self.allowed():
            return
        action = self.ACTIONS.get(urllib.parse.urlsplit(self.path).path)
        if not action:
            return self.send_error_json(404, "No such action.")
        if self.cross_site():
            return self.send_error_json(403, "Cross-origin requests are not allowed.")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self.send_error_json(415, "Send application/json.")
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or int(length) > 1024:
            return self.send_error_json(413, "Send a small JSON body with Content-Length.")
        try:
            body = json.loads(self.rfile.read(int(length)) or b"{}")
        except (OSError, ValueError):
            body = None
        if not isinstance(body, dict):
            return self.send_error_json(400, "Send a JSON object.")
        if "id" in body and not isinstance(body["id"], str):
            return self.send_error_json(400, "id must be a string.")
        status, message = action(self.copier, body)
        if status != 200:
            return self.send_error_json(status, message)
        self.send_json({"ok": True, "status": self.copier.status()})

    def cross_site(self):
        """True when a browser says the request comes from another site (CSRF)."""
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return True
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            return urllib.parse.urlsplit(origin).netloc != self.headers.get("Host", "")
        return False

    def do_PUT(self):
        self.close_connection = True
        self.send(405, b"Method not allowed\n", "text/plain; charset=utf-8", extra={"Allow": "GET, HEAD, POST"})

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, cfg, copier):
        self.cfg, self.copier = cfg, copier
        super().__init__(address, Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Xbox 360 Game Manager (Aurora)")
    parser.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    parser.add_argument("--port", type=int, help="override port")
    parser.add_argument("--check", action="store_true", help="validate the config, then exit")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log.error("config: %s", exc)
        return 2
    if args.port:
        cfg["port"] = args.port
    if not os.path.isdir(cfg["games_dir"]):
        log.error("config: games_dir %s is not a folder", cfg["games_dir"])
        return 2
    if args.check:
        print("config OK: %s:%d, games in %s" % (cfg["bind"], cfg["port"], cfg["games_dir"]))
        return 0
    copier = xbox.XboxCopier(cfg, BASE_DIR)
    httpd = Server((cfg["bind"], cfg["port"]), cfg, copier)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown).start())
    copier.start()
    log.info("Xbox 360 Game Manager %s on http://%s:%d/", VERSION, cfg["bind"], cfg["port"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        copier.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
