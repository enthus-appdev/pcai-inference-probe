# PCAI inference probe

A standalone, **CPU-only** application and Helm framework chart for testing the infrastructure around an inference server. No model downloads, CUDA dependencies, GPU requests or inference.

## What it tests

| Area | Test |
| --- | --- |
| Routing and API auth | Public landing page, authenticated diagnostics, mock `/v1/models` |
| Header forwarding | Reports only **presence**, never values, of selected auth/cookie/proxy headers |
| Streaming | Incrementally flushed SSE and mock streaming chat; configurable initial delay/event spacing |
| Proxy timeouts | Authenticated delayed responses, bounded to 600 seconds |
| Request-size limits | Streamed upload byte count and SHA-256, without echoing the body |
| Shared memory | Actual `/dev/shm` capacity/type plus bounded write-and-cleanup tests |
| Storage | Cache filesystem diagnostics, retained PVC by default, create-once persistence marker |
| Lifecycle | Separate startup/readiness/liveness probes; optional delayed readiness |
| Resource accounting | Allowlisted cgroup-v2 memory/CPU/pid counters, UID/GID and CPU affinity |

This does **not** qualify NCCL, TP2, model loading, GPU memory, inference correctness or performance. A successful SHM write establishes usable bytes in this pod, not that a later inference deployment will be admitted with the same settings.

## Security and resource bounds

- API key is mandatory. This is an application design choice, not a PCAI requirement. The chart references an existing namespace-local Secret and **does not create it**; create it before deployment. Keep the same Secret reference across upgrades. Only `/`, `/app.js`, `/healthz` and `/readyz` are public **at the application layer**; the platform gateway can still protect them.
- Supply the key through `Authorization: Bearer …` **or** `X-Probe-API-Key: …`. The separate header leaves `Authorization` available for a platform-validated JWT. The probe does not validate that JWT itself.
- No shell/exec endpoint, arbitrary file paths, outbound HTTP, environment dump, Kubernetes token mount, host access, privileged mode or application RBAC.
- Keys, cookies, prompts, request bodies, query strings and unknown path contents are not logged. Diagnostics expose only allowlisted data. The browser does not save the key in local/session storage or URLs.
- Default request limit: 8 MiB; configurable up to 32 MiB. JSON control/chat bodies are capped at 1 MiB. Chunked request bodies are intentionally unsupported; send `Content-Length`.
- Maximum 32 connection workers, four simultaneous long/payload tests, one storage operation, 120 SSE events and 600 seconds per delayed/streaming test. Writes are bounded to **128 MiB** and clean up only the temporary file they created. Failed storage checks return `ok: false` with an errno, not file contents.
- Helm defaults: one replica, **100m CPU/64 MiB requested; 1 CPU/512 MiB limited**, 2 GiB SHM tmpfs, read-only root filesystem, non-root UID/GID 65532, dropped capabilities. Resource schema rejects GPU resource keys. `NVIDIA_VISIBLE_DEVICES=void` is also set.
- A 2 GiB tmpfs is a capacity ceiling, **not** reserved memory or permission to write 2 GiB inside a 512 MiB memory cgroup. Tmpfs pages count against memory limits.

This is a temporary diagnostic application behind a managed ingress, not a general production web server. Protect the hostname, limit network exposure, remove it when finished and rotate/remove its key. General hostile traffic can still exhaust the bounded worker pool. No deployment or gateway change is performed by the build/tests.

## Build and test locally

Requirements: Python 3.13, Docker, Helm 3, Node (JS syntax check). The runtime has no pip dependencies; PyYAML is test/build tooling only.

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s probe/tests -v
python3 -m unittest discover -s checks -p 'test_*.py' -v
node --check probe/app.js
helm lint charts/pcai-inference-probe
docker build -t pcai-inference-probe:local probe
python3 checks/container_smoke.py
```

The smoke test starts/removes one **local CPU-only** container, publishes only to loopback, uses a throwaway key and writes/cleans 68,420,360 bytes (65.25 MiB) in its 2 GiB SHM mount under a 512 MiB memory cap. It does not contact PCAI or the model service.

## CI, image and importable chart

Pull requests run unit/HTTP/security/Helm contracts, build the CPU-only image and run the container smoke test. They **do not publish** an image.

After merging to `main`, the workflow publishes:

```text
ghcr.io/enthus-appdev/pcai-inference-probe:sha-<full-commit-sha>
ghcr.io/enthus-appdev/pcai-inference-probe:latest
```

The **`pcai-inference-probe-framework`** workflow artifact contains:

- A Helm `.tgz` and identical `.tar.gz` copy for UI extension compatibility.
- `SHA256SUMS` and `IMAGE.txt`.
- Source chart version plus `-ci.<workflow-run-number>` with its **published image digest pinned** in packaged defaults.

The source chart defaults to `latest`; use the CI artifact or override `image.digest` for a reproducible deployment. First publication may require an organization/package administrator to make the GHCR package public; repository visibility alone is not proof of anonymous package access. For private packages, use an authorized `imagePullSecrets` reference.

No workflow installs a chart or touches a cluster. Bump a locally packaged chart's version before reimporting changes; PCAI chart-upload handling can retain an existing version. Installed PCAI/controller behavior and admission remain deployment-time checks.

## PCAI framework preparation (operator actions, not automated)

1. Use a separate probe framework/release and hostname, not the running MLIS service. Obtain approval for this small CPU deployment, selected namespace and any storage allocation.
2. Download the **main-branch** CI artifact. Verify checksums. Do not import the ZIP wrapper; extract it and import the chart archive accepted by the installed PCAI UI.
3. Pre-create a Secret named `pcai-inference-probe-api`, key `api-key`, **in the framework release namespace**, or set `auth.existingSecret`/`auth.secretKey` to an existing authorized Secret. Use a random 32-byte-or-longer key. No inline key value is supported in Helm values; missing/unreadable/short keys fail closed.
4. Configure the actual hostname/gateway in PCAI override values. Include substitution placeholders explicitly in those overrides rather than assuming the controller substitutes chart defaults:

```yaml
auth:
  existingSecret: pcai-inference-probe-api
ezua:
  virtualService:
    enabled: true
    endpoint: "pcai-probe.${DOMAIN_NAME}"
    istioGateway: istio-system/ezaf-gateway
    timeout: 900s
  authorizationPolicy:
    enabled: false
```

`timeout: 900s` allows the probe's maximum 600-second test. The installed PCAI Istio validation rejects `0s` (even though some upstream Istio versions use zero to disable the timeout); chart validation requires more than 1 ms. Other gateway/auth/LB idle limits still apply. Use an explicit hostname instead of `${DOMAIN_NAME}` if that substitution is unsupported. Choose a controller/Helm timeout allowing the readiness delay, image pull and scheduling time; the chart's startup budget is 750 seconds and progress deadline is 1,200 seconds, independently.

If creating the key with Kubernetes tooling, **explicitly select the PCAI context**; never rely on the workstation's current context. Example for an authorized operator:

```sh
: "${PCAI_CONTEXT:?Select the PCAI context}"
: "${PCAI_NAMESPACE:?Select the framework namespace}"
umask 077
key_file=$(mktemp)
openssl rand -hex 32 > "$key_file"
kubectl --context "$PCAI_CONTEXT" --namespace "$PCAI_NAMESPACE" create secret generic pcai-inference-probe-api \
  --from-file="api-key=$key_file"
# Retain the key securely for the UI/client, then remove this private temporary file.
```

Do not commit/paste the key, put it in Helm values or include it in diagnostic exports. If the tenant cannot create the Secret, request narrowly scoped administrator assistance rather than broader privileges.

### Authentication interpretation

The default chart creates a VirtualService with explicit root-prefix matching and root-prefix rewriting, **not a gateway-auth bypass**. Its routing metadata follows the public PCAI Open WebUI chart conventions: `networking.istio.io/v1alpha3`, an explicit release namespace, a `<release>-<chart>-virtual-service` name (capped at 63 characters), and no HPE labels on the VirtualService itself. Required HPE labels remain on the pod. The chart does not install a cluster-wide Kyverno policy. These conventions are a compatibility choice, not proof of successful shared-gateway reconciliation. External TLS depends on PCAI registering this hostname and its certificate with the shared gateway. A healthy internal service does not establish external TLS readiness; inspect VirtualService/controller events if the TLS handshake fails. HTTP route matching cannot repair a missing gateway TLS listener or failed certificate-controller reconciliation. `ezua.authorizationPolicy.enabled: false` merely avoids adding a new policy; existing gateway CUSTOM/ALLOW/DENY/JWT policies remain effective.

An optional host-scoped CUSTOM AuthorizationPolicy can be rendered only after platform approval and verification of provider name, gateway selector and policy namespace. Its defaults are examples, not a verified installed platform contract. Enabling it requires cross-namespace permissions and can trigger browser OIDC authentication. No RequestAuthentication, mesh provider, OAuth2Proxy or cluster-wide policy is installed.

Use the public page's anonymous model-list button, then the keyed request:

| Observation | Meaning |
| --- | --- |
| Public HTML 200 | Landing page reachable only; says nothing about protected API access |
| Anonymous `/v1/models` returns probe JSON 401 | Request reached the app; probe key is required |
| Redirect/login response or generic 403 | Gateway/platform likely intervened; inspect policy/log evidence before attributing it |
| Keyed `/v1/models` 200 without browser cookies | Machine API-key access works for this host and request, not every platform route |
| JWT plus `X-Probe-API-Key` succeeds | Both layers can be satisfied, assuming gateway JWT validation is independently confirmed |

Use HTTPS externally. Do not follow redirects while sending a key. The browser test blocks redirects and does not display non-JSON gateway response bodies. Header-presence booleans help distinguish stripping from application rejection without exposing credentials.

### Persistent storage

By default `/var/lib/pcai-probe` uses a dedicated **1 GiB PVC** named `<release-name>-cache`, retained on chart removal. This is a small persistence qualification volume, not storage sized for large model weights. Setting `persistence.enabled: false` opts into a 256 MiB pod-local emptyDir instead; that data does not survive pod replacement.

For an authorized existing claim in the release namespace:

```yaml
persistence:
  enabled: true
  existingClaim: your-approved-probe-claim
```

Leave `existingClaim` empty to provision the default new 1 GiB PVC, explicitly selecting an approved `storageClass` if necessary. Null omits StorageClass selection; an empty string explicitly disables dynamic class selection. Access mode defaults to `ReadWriteOnce`. No model data should be placed on this diagnostic claim.

The chart-created PVC has `helm.sh/resource-policy: keep` by default. Confirm controller/storage reclaim behavior before removal. Existing claims are never chart-owned. Reattaching a retained claim should use `existingClaim` explicitly; uninstall/reimport is not an assumed rollback strategy.

Create the marker once, record its returned ID, replace the **pod** deliberately, then verify the same marker ID with a different diagnostics `instance_id`. Repeat after a Helm upgrade, keeping the same release name, namespace and claim name. The chart does not make image tags part of the PVC name. The API never overwrites an existing marker. Configuration saying `cache_expected_persistent: true` is not proof of actual durability.

For later inference deployments, use a separately sized durable model-weight claim and mount the same claim/cache path across engine-image upgrades (for example via `HF_HOME` or the engine's download-directory option). Preserve pinned model revisions so a different revision is not mistaken for a cache miss. A PVC stores model files, not container-image layers, which are cached separately by the node runtime. No model download is performed by this probe.

## API reference

All endpoints below require the key except the public UI/static/health paths described above.

| Method/path | Payload or query |
| --- | --- |
| `GET /api/report` | Sanitized resource/filesystem/header-presence diagnostics |
| `GET /v1/models` | One fake model: `pcai-inference-probe` |
| `POST /v1/chat/completions` | `{"model":"pcai-inference-probe","messages":[],"stream":true}`; fixed mock output, no fabricated token accounting |
| `GET /api/events` | `events=5&interval=1&delay=0`; interval 0–30 seconds, initial delay 0–600; combined duration ≤600 seconds |
| `GET /api/delay` | `seconds=120`; intentionally sends no headers before the delay |
| `POST /api/payload` | Any body within configured limit; returns byte count/digest only |
| `POST /api/storage/check` | `{"target":"shm","bytes":68420360}`; targets only `shm` or `cache` |
| `GET /api/persistence` | Read marker, or report absent |
| `POST /api/persistence` | Empty body with `Content-Length: 0`; create marker only if absent |
| `GET /metrics` | Minimal aggregate request counter, also key-protected |

For command-line tests, keep the key out of URLs and process arguments. For example, with a hex key entered privately:

```sh
read -rsp 'Probe key: ' PROBE_KEY; printf '\n'
printf 'header = "X-Probe-API-Key: %s"\n' "$PROBE_KEY" | \
  curl --config - --no-progress-meter --max-time 20 --fail-with-body "$PROBE_URL/api/report"
unset PROBE_KEY
```

Do not add `--location`, `--verbose` or trace options to credentialed probes. For SSE use `--no-buffer` and a client deadline longer than the chosen bounded test. Compare arrival timestamps, not just final response contents, to detect buffering. Browser fetch chunks can combine multiple SSE events; timestamps are observed chunk arrival, not exact per-event network timing.

## Runtime settings

`PROBE_API_KEY_FILE` (default `/var/run/probe-key/api-key`), `PROBE_CACHE_DIR`, `PROBE_MAX_BODY_BYTES`, `PROBE_READY_DELAY_SECONDS`, `PROBE_CACHE_PERSISTENT` and `PORT` are operator configuration. The Helm chart supplies fixed safe paths and port 8080. It intentionally has no arbitrary environment, sidecar, init-container, hostPath, RBAC or extra-resource passthrough.

The schema permits unused controller metadata at the root and under `ezua`, while strictly constraining the actual pod resource fields. Helm renders use `.Release.Name` and `.Release.Namespace` consistently; no hidden Helm `tpl` expansion is required.

Licensed under Apache-2.0.
