"""Validate pinned upstream wireproxy without TUN, privileges, or VPN traffic."""
import base64
import os
import subprocess
import tempfile
from pathlib import Path

IMAGE = "ghcr.io/windtf/wireproxy@sha256:24db66eec033a080d62ff2e39f8df36c215696053114d97e683873cbc18b7537"


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "wireproxy.conf"
        private = base64.b64encode(os.urandom(32)).decode()
        public = base64.b64encode(os.urandom(32)).decode()
        path.write_text(
            "[Interface]\nAddress = 10.44.0.2/32\nPrivateKey = " + private
            + "\nMTU = 1280\nCheckAlive = 10.44.0.1\nCheckAliveInterval = 10\n"
            + "[Peer]\nPublicKey = " + public
            + "\nEndpoint = 127.0.0.1:51820\nAllowedIPs = 10.44.0.1/32\nPersistentKeepalive = 25\n"
            + "[TCPServerTunnel]\nListenPort = 8080\nTarget = 127.0.0.1:8080\n"
        )
        path.chmod(0o444)  # Throwaway fixture only; Kubernetes Secret mounts use0440/fsGroup.
        result = subprocess.run([
            "docker", "run", "--rm", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", "10000004:1001",
            "--memory=128m", "--cpus=0.25", "--pids-limit=64",
            "--mount", f"type=bind,src={path},dst=/var/run/wireguard/wireproxy.conf,readonly",
            IMAGE, "--config", "/var/run/wireguard/wireproxy.conf", "--configtest",
        ], capture_output=True, text=True, timeout=120)
        if result.returncode or "Config OK" not in result.stdout:
            raise RuntimeError("Pinned wireproxy configuration test failed; output suppressed to protect key material")
        print("PASS pinned wireproxy: non-root arbitrary UID, read-only, no network/TUN/capabilities; reverse TCP config accepted")


if __name__ == "__main__":
    main()
