"""Bounded CPU-only framework probe. No engine, shell, arbitrary paths or outbound HTTP."""

import hashlib
import hmac
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 32 * 1024 * 1024
MAX_STORAGE_WRITE = 128 * 1024 * 1024
MAX_TEST_SECONDS = 600
CGROUP_FILES = (
    "memory.current", "memory.max", "memory.events", "cpu.max", "pids.current", "pids.max",
)
ROUTES = {
    "/", "/app.js", "/healthz", "/readyz", "/metrics", "/api/report", "/api/events",
    "/api/delay", "/api/payload", "/api/storage/check", "/api/persistence",
    "/v1/models", "/v1/chat/completions",
}


class ProbeError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


class Config:
    def __init__(self, api_key, cache_dir="/var/lib/pcai-probe", shm_dir="/dev/shm",
                 max_body=8 * 1024 * 1024, ready_delay=0, persistent=False):
        if len(api_key) < 16 or len(api_key) > 4096:
            raise ValueError("API key must contain 16 to 4096 bytes")
        if not 1 <= max_body <= MAX_BODY or not 0 <= ready_delay <= MAX_TEST_SECONDS:
            raise ValueError("Invalid body limit or readiness delay")
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.shm_dir = Path(shm_dir)
        self.max_body = max_body
        self.ready_delay = ready_delay
        self.persistent = persistent


def filesystem_info(path):
    info = {"configured_path": str(path)}
    try:
        stat = os.statvfs(path)
        info.update(total_bytes=stat.f_blocks * stat.f_frsize,
                    available_bytes=stat.f_bavail * stat.f_frsize)
        resolved = str(path.resolve())
        best = ""
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            before, after = line.split(" - ", 1)
            mount = before.split()[4].replace("\\040", " ")
            if (resolved == mount or resolved.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
                best = mount
                info["filesystem_type"] = after.split()[0]
    except (OSError, ValueError, IndexError):
        info["unavailable"] = True
    return info


class ProbeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, config):
        self.config = config
        self.started = time.monotonic()
        self.instance_id = uuid.uuid4().hex
        self.connections = threading.BoundedSemaphore(32)
        self.tests = threading.BoundedSemaphore(4)
        self.storage_lock = threading.Lock()
        self.counter_lock = threading.Lock()
        self.requests = 0
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            try:
                request.settimeout(2)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    @contextmanager
    def test_slot(self):
        if not self.tests.acquire(blocking=False):
            raise ProbeError(429, "Too many active tests (maximum four)")
        try:
            yield
        finally:
            self.tests.release()

    def report(self):
        cgroup = {}
        for name in CGROUP_FILES:
            try:
                cgroup[name] = (Path("/sys/fs/cgroup") / name).read_text()[:2048].strip()
            except OSError:
                cgroup[name] = None
        return {
            "probe": True, "model_loaded": False, "instance_id": self.instance_id,
            "uptime_seconds": round(time.monotonic() - self.started, 3),
            "uid": os.getuid(), "gid": os.getgid(),
            "cpu_affinity_count": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "cgroup_v2": cgroup,
            "shm": filesystem_info(self.config.shm_dir),
            "cache": filesystem_info(self.config.cache_dir),
            "cache_expected_persistent": self.config.persistent,
            "max_body_bytes": self.config.max_body,
            "max_storage_test_bytes": MAX_STORAGE_WRITE,
            "max_test_seconds": MAX_TEST_SECONDS,
            "proxy_configured": any(os.environ.get(n) for n in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")),
            "custom_ca_configured": any(os.environ.get(n) for n in ("SSL_CERT_FILE", "CURL_CA_BUNDLE")),
            "api_key_required": True,
        }

    def storage_check(self, target, amount):
        if target not in ("shm", "cache"):
            raise ProbeError(400, "target must be shm or cache")
        if type(amount) is not int or not 1 <= amount <= MAX_STORAGE_WRITE:
            raise ProbeError(400, "bytes must be an integer from 1 to 134217728")
        if not self.storage_lock.acquire(blocking=False):
            raise ProbeError(429, "A storage test is already running")
        path = self.config.shm_dir if target == "shm" else self.config.cache_dir
        filename = None
        result = {"target": target, "requested_bytes": amount, "written_bytes": 0,
                  "before": filesystem_info(path), "ok": False}
        try:
            fd, filename = tempfile.mkstemp(prefix="pcai-probe-", dir=path)
            with os.fdopen(fd, "wb", buffering=0) as stream:
                block = b"\0" * 65536
                while result["written_bytes"] < amount:
                    written = stream.write(block[:min(len(block), amount - result["written_bytes"])])
                    if not written:
                        raise OSError("short write")
                    result["written_bytes"] += written
                os.fsync(stream.fileno())
            result["ok"] = True
        except OSError as error:
            result["errno"] = error.errno
            result["error"] = "Storage allocation failed; no file contents returned"
        finally:
            try:
                if filename is not None:
                    os.unlink(filename)
            finally:
                self.storage_lock.release()
        result["after_cleanup"] = filesystem_info(path)
        return result

    @contextmanager
    def storage_slot(self):
        if not self.storage_lock.acquire(blocking=False):
            raise ProbeError(429, "A storage operation is already running")
        try:
            yield
        finally:
            self.storage_lock.release()

    def persistence(self, create=False):
        path = self.config.cache_dir / "probe-marker.json"
        with self.storage_slot():
            if create:
                value = {"marker": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat()}
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "w") as stream:
                        json.dump(value, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                except FileExistsError:
                    pass
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                return {"present": False, "expected_persistent": self.config.persistent}
            with os.fdopen(fd) as stream:
                value = json.loads(stream.read(4096))
            if (not isinstance(value, dict) or set(value) != {"marker", "created_at"}
                    or not isinstance(value["marker"], str)
                    or not re.fullmatch(r"[0-9a-f]{32}", value["marker"])
                    or not isinstance(value["created_at"], str)
                    or len(value["created_at"]) > 64):
                raise ProbeError(409, "Invalid probe marker; contents not returned")
            return {"present": True, "expected_persistent": self.config.persistent, **value}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PCAIProbe/1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)
        self.route_name = "<unparsed>"
        self.response_started = False

    def log_message(self, *args):
        pass

    def log_request(self, code="-", size="-"):
        # Never log query strings, bodies, headers or unknown path contents.
        print(json.dumps({"method": self.command if self.command in ("GET", "POST", "HEAD") else "other",
                          "route": self.route_name, "status": str(code)}), flush=True)

    def send_error(self, code, message=None, explain=None):
        self.json_response(code, {"error": HTTPStatus(code).phrase})

    def headers_for(self, status, content_type, length=None):
        self.response_started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="pcai-probe"')
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()
        self.close_connection = True

    def json_response(self, status, value):
        body = json.dumps(value, allow_nan=False).encode()
        self.headers_for(status, "application/json", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def authenticated(self):
        auth = self.headers.get_all("Authorization", [])
        probe_keys = self.headers.get_all("X-Probe-API-Key", [])
        if len(auth) > 1 or len(probe_keys) > 1:
            return False
        # A separate header permits a platform JWT in Authorization without
        # weakening the probe's own key check. Values are never echoed/logged.
        if probe_keys:
            return hmac.compare_digest(probe_keys[0].encode(), self.server.config.api_key)
        expected = b"Bearer " + self.server.config.api_key
        return len(auth) == 1 and hmac.compare_digest(auth[0].encode(), expected)

    def body_length(self, maximum):
        values = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding"):
            raise ProbeError(400, "Transfer-Encoding is not supported by this probe")
        if not values:
            raise ProbeError(411, "Content-Length is required")
        if len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]):
            raise ProbeError(400, "One numeric Content-Length is required")
        length = int(values[0])
        if length > maximum:
            raise ProbeError(413, "Request exceeds configured body limit")
        return length

    def read_bytes(self, length):
        data = self.rfile.read(length)
        if len(data) != length:
            raise ProbeError(400, "Incomplete request body")
        return data

    def read_json(self):
        length = self.body_length(min(self.server.config.max_body, 1024 * 1024))
        try:
            value = json.loads(self.read_bytes(length))
        except (ValueError, UnicodeError):
            raise ProbeError(400, "Invalid JSON") from None
        if not isinstance(value, dict):
            raise ProbeError(400, "Expected a JSON object")
        return value

    def number(self, query, name, default, minimum, maximum):
        try:
            values = query.get(name, [str(default)])
            if len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]):
                raise ValueError
            value = int(values[0])
            if not minimum <= value <= maximum:
                raise ValueError
            return value
        except ValueError:
            raise ProbeError(400, f"Invalid {name}; allowed range {minimum}..{maximum}") from None

    def stream(self, events, interval, delay, chat=False):
        if delay + max(0, events - 1) * interval > MAX_TEST_SECONDS:
            raise ProbeError(400, "Combined test duration exceeds 600 seconds")
        with self.server.test_slot():
            time.sleep(delay)  # Deliberate pre-header delay tests TTFT/idle limits.
            self.headers_for(200, "text/event-stream")
            started = time.monotonic()
            for index in range(events):
                if chat:
                    data = {"id": "probe-stream", "object": "chat.completion.chunk", "model": "pcai-inference-probe",
                            "probe": True, "choices": [{"index": 0, "delta": {"content": f"probe-{index} "}, "finish_reason": None}]}
                else:
                    data = {"probe": True, "sequence": index, "elapsed_seconds": round(time.monotonic() - started, 3)}
                self.wfile.write(b"data: " + json.dumps(data).encode() + b"\n\n")
                self.wfile.flush()
                if index + 1 < events:
                    time.sleep(interval)
            if chat:
                end = {"id": "probe-stream", "object": "chat.completion.chunk", "model": "pcai-inference-probe", "probe": True,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(b"data: " + json.dumps(end).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        try:
            path = urlsplit(self.path).path
            self.route_name = path if path in ROUTES else "<unknown>"
            with self.server.counter_lock:
                self.server.requests += 1
            if path not in ROUTES:
                raise ProbeError(404, "Unknown endpoint")
            if self.command in ("GET", "HEAD") and path in ("/healthz", "/readyz"):
                ready = path == "/healthz" or time.monotonic() - self.server.started >= self.server.config.ready_delay
                self.json_response(200 if ready else 503, {"probe": True, "status": "ok" if ready else "starting"})
                return
            if self.command in ("GET", "HEAD") and path in ("/", "/app.js"):
                filename, kind = ("index.html", "text/html; charset=utf-8") if path == "/" else ("app.js", "text/javascript; charset=utf-8")
                data = (Path(__file__).parent / filename).read_bytes()
                self.headers_for(200, kind, len(data))
                if self.command != "HEAD":
                    self.wfile.write(data)
                return
            if not self.authenticated():
                raise ProbeError(401, "A valid probe API key is required")
            if self.command == "HEAD":
                self.headers_for(200, "application/json", 0)
                return
            try:
                query = parse_qs(urlsplit(self.path).query, max_num_fields=16)
            except ValueError:
                raise ProbeError(400, "Too many query fields") from None
            if self.command == "GET":
                if path == "/api/report":
                    report = self.server.report()
                    report["request_header_presence"] = {
                        name: bool(self.headers.get(name))
                        for name in ("Authorization", "X-Probe-API-Key", "Cookie", "X-Forwarded-Proto", "X-Request-Id")
                    }
                    self.json_response(200, report)
                elif path == "/metrics":
                    data = f"# TYPE pcai_probe_requests_total counter\npcai_probe_requests_total {self.server.requests}\n".encode()
                    self.headers_for(200, "text/plain; version=0.0.4", len(data))
                    self.wfile.write(data)
                elif path == "/api/persistence":
                    self.json_response(200, self.server.persistence())
                elif path == "/v1/models":
                    self.json_response(200, {"object": "list", "data": [{"id": "pcai-inference-probe", "object": "model", "created": 0, "owned_by": "cpu-probe"}]})
                elif path in ("/api/events", "/api/delay"):
                    delay = self.number(query, "delay", 0, 0, MAX_TEST_SECONDS)
                    if path == "/api/delay":
                        seconds = self.number(query, "seconds", 1, 0, MAX_TEST_SECONDS)
                        with self.server.test_slot():
                            time.sleep(seconds)
                            self.json_response(200, {"probe": True, "delayed_seconds": seconds})
                    else:
                        self.stream(self.number(query, "events", 5, 1, 120), self.number(query, "interval", 1, 0, 30), delay)
                else:
                    raise ProbeError(405, "Method not allowed")
            elif self.command == "POST":
                if path == "/api/payload":
                    length = self.body_length(self.server.config.max_body)
                    digest = hashlib.sha256()
                    remaining = length
                    with self.server.test_slot():
                        while remaining:
                            data = self.read_bytes(min(65536, remaining))
                            digest.update(data)
                            remaining -= len(data)
                    self.json_response(200, {"received_bytes": length, "sha256": digest.hexdigest(), "body_echoed": False})
                elif path == "/api/storage/check":
                    value = self.read_json()
                    with self.server.test_slot():
                        self.json_response(200, self.server.storage_check(value.get("target", "shm"), value.get("bytes", 34210180)))
                elif path == "/api/persistence":
                    self.body_length(0)
                    self.json_response(200, self.server.persistence(create=True))
                elif path == "/v1/chat/completions":
                    value = self.read_json()
                    if not isinstance(value.get("messages"), list) or type(value.get("stream", False)) is not bool:
                        raise ProbeError(400, "Expected messages array and boolean stream")
                    if value.get("model", "pcai-inference-probe") != "pcai-inference-probe":
                        raise ProbeError(400, "Only the mock pcai-inference-probe model exists")
                    if value.get("stream", False):
                        self.stream(5, 1, 0, chat=True)
                    else:
                        self.json_response(200, {"id": "probe-completion", "object": "chat.completion", "model": "pcai-inference-probe", "probe": True,
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": "CPU-only probe: no model was loaded."}, "finish_reason": "stop"}]})
                else:
                    raise ProbeError(405, "Method not allowed")
        except ProbeError as error:
            if not self.response_started:
                self.json_response(error.status, {"error": error.message})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception as error:
            print(json.dumps({"error_type": type(error).__name__, "route": self.route_name}), flush=True)
            if not self.response_started:
                self.json_response(500, {"error": "Probe operation failed; no sensitive details returned"})


def main():
    try:
        key = Path(os.environ.get("PROBE_API_KEY_FILE", "/var/run/probe-key/api-key")).read_bytes().strip()
        config = Config(key, cache_dir=os.environ.get("PROBE_CACHE_DIR", "/var/lib/pcai-probe"),
                        max_body=int(os.environ.get("PROBE_MAX_BODY_BYTES", 8 * 1024 * 1024)),
                        ready_delay=int(os.environ.get("PROBE_READY_DELAY_SECONDS", 0)),
                        persistent=os.environ.get("PROBE_CACHE_PERSISTENT", "false").lower() == "true")
        config.cache_dir.mkdir(parents=True, exist_ok=True)
        server = ProbeServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), config)
    except (OSError, ValueError):
        print("Probe startup failed: check key file, cache permissions and numeric settings (values not logged)", file=sys.stderr)
        return 2
    print("CPU-only PCAI probe listening; API key required; no GPU or model initialization", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
