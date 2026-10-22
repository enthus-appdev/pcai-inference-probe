"""CPU-only local Docker smoke test; no Kubernetes access or GPU flags."""
import argparse
import hashlib
import http.client
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def smoke(image):
    container = None
    with tempfile.TemporaryDirectory() as directory:
        secret = Path(directory) / "api-key"
        secret.write_text(os.urandom(32).hex())
        secret.chmod(0o444)
        try:
            container = docker("run", "--detach", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--memory=512m", "--cpus=1", "--pids-limit=128",
                "--shm-size=2g", "--publish", "127.0.0.1::8080",
                "--mount", f"type=bind,src={secret},dst=/var/run/probe-key/api-key,readonly",
                "--tmpfs", "/var/lib/pcai-probe:rw,noexec,nosuid,size=268435456,uid=65532,gid=65532",
                image)
            meta = json.loads(docker("inspect", container))[0]
            assert not meta["HostConfig"].get("DeviceRequests"), "Unexpected device request"
            assert not meta["HostConfig"].get("Devices"), "Unexpected device mapping"
            port = int(meta["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"])

            def request(path, method="GET", value=None, auth=True):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                headers = {"X-Probe-API-Key": secret.read_text()} if auth else {}
                body = json.dumps(value).encode() if value is not None else (b"" if method == "POST" else None)
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                status, data = response.status, response.read()
                conn.close()
                return status, data

            for attempt in range(50):
                try:
                    if request("/healthz", auth=False)[0] == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("Container did not become healthy within five seconds")
            assert request("/v1/models", auth=False)[0] == 401
            status, body = request("/api/report")
            report = json.loads(body)
            assert status == 200 and report["uid"] == 65532
            assert report["shm"]["total_bytes"] == 2 * 1024**3
            assert report["shm"]["filesystem_type"] == "tmpfs"
            assert not report["model_loaded"]
            assert secret.read_text().encode() not in body
            # Two 34,210,180-byte allocations exceed Docker's default 64 MiB.
            status, body = request("/api/storage/check", "POST", {"target": "shm", "bytes": 68420360})
            result = json.loads(body)
            assert status == 200 and result["ok"] and result["written_bytes"] == 68420360
            assert request("/api/events?events=2&interval=0")[1].endswith(b"data: [DONE]\n\n")
            status, body = request("/v1/chat/completions", "POST", {"messages": [], "stream": False})
            assert status == 200 and json.loads(body)["probe"]
            assert request("/api/persistence", "POST")[0] == 200
            assert json.loads(request("/api/persistence")[1])["present"]
            print("PASS CPU-only container: non-root/read-only/512MiB, API auth, 2GiB SHM, real 65.25MiB write+cleanup, SSE, mock OpenAI, cache marker")
        finally:
            if container:
                docker("rm", "--force", container)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="pcai-inference-probe:local")
    smoke(parser.parse_args().image)
