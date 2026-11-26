import http.client
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/token-monitor"))
from relay import relay  # noqa: E402


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=3)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class RelayTest(unittest.TestCase):
    def test_only_expected_ingest_request_reaches_hub(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "server.sock"
            hub = Mock()
            hub.request.return_value = (200, b'{"ok":true,"deviceId":"fixture","extra":"discard"}')
            with relay(path, hub, "fixture") as server:
                for route, device in [("/api/devices", "fixture"), ("/api/ingest", "another")]:
                    connection = UnixConnection(path)
                    connection.request("POST", route, json.dumps({"deviceId": device}))
                    with connection.getresponse() as response:
                        self.assertIn(response.status, {400, 404})
                    connection.close()
                self.assertFalse(server.acknowledged)
                hub.request.assert_not_called()

                body = b'{"deviceId":"fixture"}'
                connection = UnixConnection(path)
                connection.request("POST", "/api/ingest", body, {"Authorization": "sandbox-value"})
                with connection.getresponse() as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read()), {"ok": True, "deviceId": "fixture"})
                connection.close()
                self.assertTrue(server.acknowledged)
                hub.request.assert_called_once_with("POST", "/api/ingest", body)

    def test_redirects_and_invalid_acks_are_errors(self):
        for code, body in [(302, b"{}"), (200, b'{"ok":true,"deviceId":"someone-else"}')]:
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "server.sock"
                hub = Mock()
                hub.request.return_value = (code, body)
                with relay(path, hub, "fixture") as server:
                    connection = UnixConnection(path)
                    connection.request("POST", "/api/ingest", b'{"deviceId":"fixture"}')
                    with connection.getresponse() as response:
                        self.assertEqual(response.status, 502)
                    connection.close()
                    self.assertFalse(server.acknowledged)


if __name__ == "__main__":
    unittest.main()
