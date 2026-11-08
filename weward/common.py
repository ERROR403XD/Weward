import json
import logging
import sqlite3
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encode_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@contextmanager
def database(path):
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_database(path, schema):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with database(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(schema)


def request_json(url, body=None, headers=None, timeout=45):
    request = urllib.request.Request(
        url,
        data=None if body is None else encode_json(body),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, message, *args):
        if self.path not in ("/healthz", "/api/status"):
            logging.info("http %s %s", self.address_string(), message % args)

    def send_bytes(self, content, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, value, status=200):
        self.send_bytes(encode_json(value), "application/json; charset=utf-8", status)

    def read_json(self, limit=64 * 1024 * 1024):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= limit:
            raise ValueError("需要有效的 Content-Length，且请求不能超过 64 MiB")
        content = self.rfile.read(length)
        if len(content) != length:
            raise ValueError("请求正文不完整")
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("JSON 正文必须为对象")
        return value

    def static(self, filename):
        content = (Path(__file__).parent / "static" / filename).read_bytes()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml; charset=utf-8",
            ".png": "image/png",
            ".ico": "image/vnd.microsoft.icon",
            ".webmanifest": "application/manifest+json; charset=utf-8",
        }[Path(filename).suffix]
        self.send_bytes(content, content_type)

    def serve_brand_asset(self, path):
        if path not in {
            "/favicon.svg", "/favicon.ico", "/apple-touch-icon.png", "/site.webmanifest",
            "/icons/weward.svg", "/icons/icon-192.png", "/icons/icon-512.png",
            "/icons/icon-maskable-512.png",
        }:
            return False
        self.static(path.removeprefix("/"))
        return True


def make_server(address, handler, app):
    server = ThreadingHTTPServer(address, handler)
    server.daemon_threads = True
    server.app = app
    return server
