import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("probe_app", ROOT / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
KEY = b"unit-test-key-not-a-production-secret"


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.cache = Path(cls.temp.name) / "cache"
        cls.shm = Path(cls.temp.name) / "shm"
        cls.cache.mkdir()
        cls.shm.mkdir()
        cls.config = app.Config(KEY, cache_dir=cls.cache, shm_dir=cls.shm, max_body=1024 * 1024)
        cls.server = app.ProbeServer(("127.0.0.1", 0), cls.config)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def request(self, path, method="GET", body=None, authenticated=True, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        h = {"Authorization": "Bearer " + KEY.decode()} if authenticated else {}
        h.update(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=h)
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        conn.close()
        return result

    def json(self, path, **kwargs):
        status, headers, body = self.request(path, **kwargs)
        return status, json.loads(body)

    def test_public_health_and_page(self):
        for path in ["/healthz", "/readyz", "/", "/app.js"]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, authenticated=False)[0], 200)

    def test_protected_endpoints_fail_closed(self):
        for path in ["/api/report", "/api/events", "/api/delay", "/api/persistence", "/metrics", "/v1/models"]:
            with self.subTest(path=path):
                status, headers, _ = self.request(path, authenticated=False)
                self.assertEqual(status, 401)
                self.assertIn("WWW-Authenticate", headers)
        for path in ["/api/storage/check", "/api/persistence", "/api/payload", "/v1/chat/completions"]:
            self.assertEqual(self.request(path, method="POST", body={}, authenticated=False)[0], 401)

    def test_wrong_and_duplicate_authorization(self):
        self.assertEqual(self.request("/api/report", headers={"Authorization": "Bearer wrong"})[0], 401)
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.putrequest("GET", "/api/report")
        conn.putheader("Authorization", "Bearer " + KEY.decode())
        conn.putheader("Authorization", "Bearer " + KEY.decode())
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        conn.close()

    def test_safe_report_no_secret_values(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://user:private-password@example.invalid", "HF_TOKEN": "sensitive-test-value"}):
            status, value = self.json("/api/report")
        self.assertEqual(status, 200)
        encoded = json.dumps(value)
        for secret in [KEY.decode(), "private-password", "sensitive-test-value"]:
            self.assertNotIn(secret, encoded)
        self.assertFalse(value["model_loaded"])
        self.assertTrue(value["proxy_configured"])
        self.assertIn("memory.max", value["cgroup_v2"])
        self.assertIn("available_bytes", value["shm"])

    def test_separate_probe_key_allows_platform_authorization_header(self):
        status, value = self.json("/api/report", headers={
            "Authorization": "Bearer platform-token-must-not-be-echoed",
            "X-Probe-API-Key": KEY.decode(), "Cookie": "private-cookie-must-not-be-echoed",
        })
        self.assertEqual(status, 200)
        self.assertTrue(value["request_header_presence"]["Authorization"])
        self.assertTrue(value["request_header_presence"]["X-Probe-API-Key"])
        self.assertNotIn("platform-token-must-not-be-echoed", json.dumps(value))
        self.assertNotIn("private-cookie-must-not-be-echoed", json.dumps(value))
        self.assertEqual(self.request("/api/report", headers={"X-Probe-API-Key": "wrong"})[0], 401)

    def test_mock_chat_stream(self):
        with patch.object(app.time, "sleep", return_value=None):
            status, headers, data = self.request("/v1/chat/completions", method="POST", body={"messages": [], "stream": True})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        chunks = [json.loads(line[6:]) for line in data.splitlines() if line.startswith(b"data: {")]
        self.assertTrue(all(c["probe"] for c in chunks))
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertTrue(data.endswith(b"data: [DONE]\n\n"))

    def test_readiness_delay_does_not_break_liveness(self):
        self.config.ready_delay = 600
        try:
            self.assertEqual(self.request("/readyz")[0], 503)
            self.assertEqual(self.request("/healthz")[0], 200)
        finally:
            self.config.ready_delay = 0

    def test_payload_digest_no_echo(self):
        body = b"private-input-is-not-echoed" * 16
        status, value = self.json("/api/payload", method="POST", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(value["received_bytes"], len(body))
        self.assertEqual(value["sha256"], hashlib.sha256(body).hexdigest())
        self.assertFalse(value["body_echoed"])
        self.assertNotIn("private-input", json.dumps(value))

    def test_oversized_body_rejected_before_reading(self):
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.putrequest("POST", "/api/payload")
        conn.putheader("Authorization", "Bearer " + KEY.decode())
        conn.putheader("Content-Length", str(self.config.max_body + 1))
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        conn.close()

    def test_transfer_encoding_and_duplicate_length_rejected(self):
        for headers in [[("Transfer-Encoding", "chunked")], [("Content-Length", "0"), ("Content-Length", "0")]]:
            conn = http.client.HTTPConnection(*self.server.server_address)
            conn.putrequest("POST", "/api/payload")
            conn.putheader("Authorization", "Bearer " + KEY.decode())
            for name, value in headers:
                conn.putheader(name, value)
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            conn.close()

    def test_missing_length_rejected(self):
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.putrequest("POST", "/api/payload")
        conn.putheader("Authorization", "Bearer " + KEY.decode())
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 411)
        response.read()
        conn.close()

    def test_truncated_body_returns_400(self):
        sock = socket.create_connection(self.server.server_address, timeout=5)
        sock.sendall(b"POST /api/payload HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + KEY + b"\r\nContent-Length: 10\r\n\r\nshort")
        sock.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(sock)
        response.begin()
        self.assertEqual(response.status, 400)
        response.read()
        sock.close()

    def test_invalid_json_and_unbounded_query_rejected(self):
        self.assertEqual(self.request("/api/storage/check", method="POST", body=b"{")[0], 400)
        self.assertEqual(self.request("/api/storage/check", method="POST", body=b"[]")[0], 400)
        self.assertEqual(self.request("/api/events?" + "&".join(f"x{i}=1" for i in range(17)))[0], 400)

    def test_storage_write_is_bounded_and_cleaned(self):
        for target in ["shm", "cache"]:
            status, value = self.json("/api/storage/check", method="POST", body={"target": target, "bytes": 100001})
            self.assertEqual(status, 200)
            self.assertTrue(value["ok"])
            self.assertEqual(value["written_bytes"], 100001)
        self.assertFalse(list(self.shm.glob("pcai-probe-*")))
        self.assertFalse(list(self.cache.glob("pcai-probe-*")))

    def test_storage_path_and_size_cannot_be_chosen_arbitrarily(self):
        for value in [{"target": "/etc"}, {"target": "../etc"}, {"bytes": 0}, {"bytes": True}, {"bytes": app.MAX_STORAGE_WRITE + 1}]:
            self.assertEqual(self.request("/api/storage/check", method="POST", body=value)[0], 400)

    def test_storage_error_is_sanitized(self):
        with patch.object(app.tempfile, "mkstemp", side_effect=OSError(28, "private-storage-detail")):
            status, value = self.json("/api/storage/check", method="POST", body={"bytes": 10})
        self.assertEqual(status, 200)
        self.assertFalse(value["ok"])
        self.assertEqual(value["errno"], 28)
        self.assertNotIn("private-storage-detail", json.dumps(value))

    def test_storage_concurrency_rejected(self):
        self.server.storage_lock.acquire()
        try:
            self.assertEqual(self.request("/api/storage/check", method="POST", body={"bytes": 10})[0], 429)
            self.assertEqual(self.request("/api/persistence")[0], 429)
        finally:
            self.server.storage_lock.release()

    def test_persistence_marker_survives_new_server_instance(self):
        marker = self.cache / "probe-marker.json"
        marker.unlink(missing_ok=True)
        self.assertFalse(self.json("/api/persistence")[1]["present"])
        status, value = self.json("/api/persistence", method="POST", body=b"")
        self.assertEqual(status, 200)
        second = app.ProbeServer(("127.0.0.1", 0), self.config)
        try:
            self.assertEqual(value["marker"], second.persistence()["marker"])
            self.assertEqual(value["marker"], self.json("/api/persistence", method="POST", body=b"")[1]["marker"])
        finally:
            second.server_close()
            marker.unlink(missing_ok=True)

    def test_persistence_symlink_is_not_followed(self):
        marker = self.cache / "probe-marker.json"
        marker.unlink(missing_ok=True)
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text('{"marker":"external-secret"}')
        marker.symlink_to(outside)
        try:
            status, _, body = self.request("/api/persistence")
            self.assertEqual(status, 500)
            self.assertNotIn(b"external-secret", body)
        finally:
            marker.unlink()

    def test_events_stream_and_duration_limits(self):
        status, headers, body = self.request("/api/events?events=2&interval=0")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertEqual(body.count(b"data: "), 3)
        self.assertIn(b"[DONE]", body)
        for query in ["events=121", "interval=31", "delay=601", "events=120&interval=30", "events=-1", "events=1&events=2"]:
            self.assertEqual(self.request("/api/events?" + query)[0], 400)

    def test_events_flush_incrementally(self):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        conn.request("GET", "/api/events?events=2&interval=1", headers={"Authorization": "Bearer " + KEY.decode()})
        response = conn.getresponse()
        start = time.monotonic()
        self.assertIn(b'"sequence": 0', response.readline())
        self.assertLess(time.monotonic() - start, 0.7)
        self.assertIn(b'"sequence": 1', response.read())
        self.assertGreaterEqual(time.monotonic() - start, 0.8)
        conn.close()

    def test_slow_test_limit_reserves_health_capacity(self):
        for _ in range(4):
            self.server.tests.acquire()
        try:
            self.assertEqual(self.request("/api/delay?seconds=0")[0], 429)
            self.assertEqual(self.request("/healthz", authenticated=False)[0], 200)
        finally:
            for _ in range(4):
                self.server.tests.release()

    def test_mock_openai_is_explicitly_fake(self):
        status, models = self.json("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(models["data"][0]["id"], "pcai-inference-probe")
        body = {"model": "pcai-inference-probe", "messages": [{"role": "user", "content": "private prompt"}]}
        status, value = self.json("/v1/chat/completions", method="POST", body=body)
        self.assertEqual(status, 200)
        self.assertTrue(value["probe"])
        self.assertNotIn("private prompt", json.dumps(value))
        self.assertNotIn("usage", value)  # No fabricated model token accounting.
        self.assertEqual(self.request("/v1/chat/completions", method="POST", body={"messages": [], "model": "real-model"})[0], 400)

    def test_no_query_or_unknown_path_in_logs(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.request("/api/report?api_key=not-to-be-logged")
            self.request("/private-path-secret")
        self.assertNotIn("not-to-be-logged", out.getvalue())
        self.assertNotIn("private-path-secret", out.getvalue())
        self.assertNotIn(KEY.decode(), out.getvalue())

    def test_security_headers_and_head(self):
        status, headers, body = self.request("/v1/models", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Connection"], "close")

    def test_configuration_bounds(self):
        for options in [{"api_key": b"short"}, {"max_body": app.MAX_BODY + 1}, {"ready_delay": 601}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                app.Config(**({"api_key": KEY} | options))


if __name__ == "__main__":
    unittest.main()
