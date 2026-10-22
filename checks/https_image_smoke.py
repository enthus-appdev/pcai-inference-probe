"""CPU-only startup check of the HTTPS transport at an arbitrary admitted UID."""
import argparse
import json
import subprocess
import time


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def main(image):
    container = None
    try:
        version = docker("run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
                         "--security-opt=no-new-privileges", "--user", "10000004:1001", image, "--version")
        assert "11.1.0" in version.stdout
        container = docker("run", "--detach", "--network=none", "--read-only", "--cap-drop=ALL",
                           "--security-opt=no-new-privileges", "--user", "10000004:1001",
                           "--memory=128m", "--cpus=0.25", "--pids-limit=64", image,
                           "server", "--restrict-to", "127.0.0.1:51820",
                           "--restrict-http-upgrade-path-prefix", "pcai-wireguard",
                           "--log-lvl", "WARN", "ws://127.0.0.1:18081").stdout.strip()
        for _ in range(50):
            state = json.loads(docker("inspect", container).stdout)[0]["State"]
            if not state["Running"]:
                raise RuntimeError("HTTPS transport server exited during startup")
            result = docker("exec", container, "/bin/sh", "-c",
                            "grep -q '0100007F:46A1 .* 0A ' /proc/net/tcp", check=False)
            if result.returncode == 0:
                print("PASS HTTPS transport: arbitrary non-root UID, read-only/no-network/no-caps; restricted loopback listener started")
                return
            time.sleep(0.1)
        raise RuntimeError("HTTPS transport did not open its loopback listener")
    finally:
        if container:
            docker("rm", "--force", container)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="pcai-wg-https:local")
    main(parser.parse_args().image)
