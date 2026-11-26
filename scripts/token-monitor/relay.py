"""Expose only the upstream ingest operation to the networkless collector."""

import http.client
import json
import os
import socketserver
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

MAX_UPLOAD = 2 * 1024 * 1024
MAX_ACK = 64 * 1024
HTTP_TIMEOUT = 8


class HubError(Exception):
    pass


class Hub:
    def __init__(self, url, secret):
        self.url = urlsplit(url)
        self.secret = secret

    def request(self, method, route, body=None):
        connection = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
        conn = connection(self.url.hostname, self.url.port, timeout=HTTP_TIMEOUT)
        try:
            conn.request(method, self.url.path.rstrip("/") + route, body=body, headers={
                "Authorization": f"Bearer {self.secret}",
                "Content-Type": "application/json",
                "X-Token-Monitor-Response": "minimal",
            })
            with conn.getresponse() as response:
                return response.status, response.read(MAX_ACK + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise HubError("Hub connection failed. Check host DNS, routing and TLS.") from exc
        finally:
            conn.close()

    def probe(self):
        status, body = self.request("GET", "/api/sync/content")
        try:
            content = json.loads(body)
        except (ValueError, RecursionError):
            content = None
        if status != 200 or not isinstance(content, dict) or "sessionTitles" not in content:
            raise HubError(f"Hub capability check failed (HTTP {status}). Check the URL, secret and Hub version.")


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(HTTP_TIMEOUT)
        super().setup()

    def log_message(self, *_args):
        pass

    def reply(self, status, body=b"{}"):
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # The upstream one-shot collector uses full ingest. Never relay
        # credentials, arbitrary endpoints, redirects, or other device IDs.
        if self.path != "/api/ingest":
            self.reply(404)
            return
        length = self.headers.get("Content-Length", "")
        if self.headers.get("Transfer-Encoding") or not length.isascii() or not length.isdecimal() or len(length) > 8:
            self.reply(400)
            return
        if int(length) > MAX_UPLOAD:
            self.reply(413)
            return
        body = self.rfile.read(int(length))
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict) or data.get("deviceId") != self.server.device:
            self.reply(400)
            return
        try:
            status, ack_body = self.server.hub.request("POST", "/api/ingest", body)
            self.server.last_status = status
        except HubError:
            self.server.last_status = 502
            self.reply(502)
            return
        if status == 200:
            try:
                ack = json.loads(ack_body)
            except (ValueError, RecursionError):
                ack = None
            if isinstance(ack, dict) and ack.get("ok") is True and ack.get("deviceId") == self.server.device:
                self.server.acknowledged = True
                self.reply(200, json.dumps({"ok": True, "deviceId": self.server.device}).encode())
                return
        self.reply(status if 400 <= status <= 599 else 502)


class Server(socketserver.UnixStreamServer):
    def __init__(self, path, hub, device):
        self.hub = hub
        self.device = device
        self.acknowledged = False
        self.last_status = None
        super().__init__(str(path), Handler)

    def handle_error(self, *_args):
        # Parser failures and disconnected clients carry no data into logs.
        pass


@contextmanager
def relay(path, hub, device):
    with Server(path, hub, device) as server:
        os.chmod(path, 0o666)  # Parent is private to the host job and mounted only in the collector.
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            path.unlink()
