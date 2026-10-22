"""Offline Helm contracts; never contacts or installs into a cluster."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts/pcai-inference-probe"


def render(values=None, success=True):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "values.json"
        path.write_text(json.dumps(values or {}))
        result = subprocess.run(
            ["helm", "template", "probe-test", str(CHART), "--namespace", "probe-namespace", "-f", str(path)],
            capture_output=True, text=True, check=False,
        )
    if success:
        if result.returncode:
            raise AssertionError(result.stderr)
        return [d for d in yaml.safe_load_all(result.stdout) if d]
    if not result.returncode:
        raise AssertionError("Invalid values unexpectedly rendered")
    return result.stderr


class ChartTests(unittest.TestCase):
    def test_cpu_only_restricted_defaults(self):
        docs = render()
        self.assertEqual({d["kind"] for d in docs}, {"Deployment", "Service", "VirtualService"})
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        spec = deployment["spec"]["template"]["spec"]
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertNotIn("hostIPC", spec)
        self.assertNotIn("hostNetwork", spec)
        self.assertNotIn("runtimeClassName", spec)
        self.assertNotIn("initContainers", spec)
        container = spec["containers"][0]
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertTrue(spec["securityContext"]["runAsNonRoot"])
        self.assertEqual(spec["securityContext"]["runAsUser"], 65532)
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        for quantities in container["resources"].values():
            self.assertEqual(set(quantities), {"cpu", "memory", "ephemeral-storage"})
        volumes = {v["name"]: v for v in spec["volumes"]}
        self.assertEqual(volumes["shm"]["emptyDir"], {"medium": "Memory", "sizeLimit": "2Gi"})
        self.assertIn("emptyDir", volumes["cache"])
        self.assertEqual(volumes["probe-key"]["secret"]["secretName"], "pcai-inference-probe-api")
        self.assertEqual(volumes["probe-key"]["secret"]["defaultMode"], 0o440)
        self.assertEqual(container["startupProbe"]["httpGet"]["path"], "/readyz")
        self.assertGreater(container["startupProbe"]["failureThreshold"] * container["startupProbe"]["periodSeconds"], 600)

    def test_numeric_environment_is_decimal_not_scientific_notation(self):
        for values, expected_body, expected_delay in [
            ({}, "8388608", "0"),
            ({"maxBodyBytes": 8.388608e6, "readyDelaySeconds": 6e2}, "8388608", "600"),
            ({"maxBodyBytes": 33554432, "readyDelaySeconds": 600}, "33554432", "600"),
        ]:
            with self.subTest(values=values):
                deployment = next(d for d in render(values) if d["kind"] == "Deployment")
                container = deployment["spec"]["template"]["spec"]["containers"][0]
                env = {e["name"]: e["value"] for e in container["env"]}
                self.assertEqual(env["PROBE_MAX_BODY_BYTES"], expected_body)
                self.assertEqual(env["PROBE_READY_DELAY_SECONDS"], expected_delay)
                self.assertEqual(str(int(env["PROBE_MAX_BODY_BYTES"])), expected_body)
                self.assertEqual(str(int(env["PROBE_READY_DELAY_SECONDS"])), expected_delay)

    def test_route_is_release_namespace_aware(self):
        docs = render({"ezua": {"virtualService": {"endpoint": "probe.example.invalid"}}})
        route = next(d for d in docs if d["kind"] == "VirtualService")
        self.assertEqual(route["spec"]["hosts"], ["probe.example.invalid"])
        self.assertEqual(route["spec"]["http"][0]["route"][0]["destination"]["host"], "probe-test.probe-namespace.svc.cluster.local")
        self.assertEqual(route["spec"]["http"][0]["timeout"], "900s")

    def test_route_timeout_requires_more_than_one_millisecond(self):
        for timeout in ["0s", "0ms", "1ms", "-1s", "invalid"]:
            with self.subTest(timeout=timeout):
                render({"ezua": {"virtualService": {"timeout": timeout}}}, success=False)
        for timeout in ["2ms", "10ms", "1s", "900s", "15m", "1h"]:
            with self.subTest(timeout=timeout):
                render({"ezua": {"virtualService": {"timeout": timeout}}})

    def test_local_no_istio_render(self):
        self.assertEqual({d["kind"] for d in render({"ezua": {"virtualService": {"enabled": False}}})}, {"Deployment", "Service"})

    def test_new_pvc_is_retained(self):
        docs = render({"persistence": {"enabled": True, "storageClass": "test-storage"}})
        pvc = next(d for d in docs if d["kind"] == "PersistentVolumeClaim")
        self.assertEqual(pvc["metadata"]["annotations"]["helm.sh/resource-policy"], "keep")
        self.assertEqual(pvc["spec"]["storageClassName"], "test-storage")

    def test_existing_pvc_does_not_create_claim(self):
        docs = render({"persistence": {"enabled": True, "existingClaim": "existing-test"}})
        self.assertNotIn("PersistentVolumeClaim", {d["kind"] for d in docs})
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        cache = next(v for v in deployment["spec"]["template"]["spec"]["volumes"] if v["name"] == "cache")
        self.assertEqual(cache["persistentVolumeClaim"]["claimName"], "existing-test")

    def test_empty_storage_class_is_explicit(self):
        pvc = next(d for d in render({"persistence": {"enabled": True, "storageClass": ""}}) if d["kind"] == "PersistentVolumeClaim")
        self.assertEqual(pvc["spec"]["storageClassName"], "")

    def test_optional_auth_is_host_scoped(self):
        docs = render({"ezua": {"authorizationPolicy": {"enabled": True}, "virtualService": {"endpoint": "probe.example.invalid"}}})
        policy = next(d for d in docs if d["kind"] == "AuthorizationPolicy")
        self.assertEqual(policy["spec"]["action"], "CUSTOM")
        self.assertEqual(policy["spec"]["rules"][0]["to"][0]["operation"]["hosts"], ["probe.example.invalid"])
        self.assertEqual(policy["metadata"]["namespace"], "istio-system")

    def test_gpu_resources_and_bad_limits_rejected(self):
        for values in [
            {"resources": {"limits": {"nvidia.com/gpu": 1}}},
            {"resources": {"requests": {"nvidia.com/gpu": 0}}},
            {"replicaCount": 2}, {"maxBodyBytes": 33554433}, {"readyDelaySeconds": 601},
            {"auth": {"existingSecret": ""}}, {"image": {"digest": "sha256:bad"}},
        ]:
            with self.subTest(values=values):
                render(values, success=False)

    def test_controller_metadata_is_accepted_but_not_rendered(self):
        docs = render({"reconfigureTimestamp": "test", "ezua": {"retries": {"lastRetryTimestamp": "test"}}})
        self.assertEqual(len(docs), 3)

    def test_image_digest_overrides_tag(self):
        digest = "sha256:" + "a" * 64
        deployment = next(d for d in render({"image": {"digest": digest}}) if d["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["template"]["spec"]["containers"][0]["image"], "ghcr.io/enthus-appdev/pcai-inference-probe@" + digest)


if __name__ == "__main__":
    unittest.main()
