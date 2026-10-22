"use strict";
const output = document.getElementById("output");
const key = document.getElementById("key");
async function test(action) {
  output.textContent = "Running…";
  const headers = {};
  if (action !== "anonymous") headers["X-Probe-API-Key"] = key.value;
  let path = "/api/report", method = "GET", body;
  if (action === "models" || action === "anonymous") path = "/v1/models";
  if (action === "stream") path = "/api/events?events=5&interval=1";
  if (action === "marker" || action === "create") path = "/api/persistence";
  if (action === "create") method = "POST";
  if (action === "shm") {
    path = "/api/storage/check"; method = "POST";
    headers["Content-Type"] = "application/json";
    body = JSON.stringify({target: "shm", bytes: 34210180});
  }
  if (action === "payload") {
    path = "/api/payload"; method = "POST";
    headers["Content-Type"] = "application/octet-stream";
    body = new Uint8Array(32768);
  }
  const start = performance.now();
  const response = await fetch(path, {method, headers, body, redirect: "error", cache: "no-store"});
  output.textContent = "HTTP " + response.status + "\n";
  if (action === "stream" && response.ok) {
    const reader = response.body.getReader(), decoder = new TextDecoder();
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      output.textContent += "[" + ((performance.now() - start) / 1000).toFixed(3) + "s] " + decoder.decode(value, {stream: true});
    }
  } else {
    const text = await response.text();
    try { output.textContent += JSON.stringify(JSON.parse(text), null, 2); }
    catch { output.textContent += "Non-JSON response: check the gateway/login flow (body not displayed)."; }
  }
}
for (const button of document.querySelectorAll("button[data-action]")) {
  button.addEventListener("click", () => {
    button.disabled = true;
    test(button.dataset.action).catch(() => {
      output.textContent = "Request failed or a redirect was blocked. Check the gateway/authentication path.";
    }).finally(() => { button.disabled = false; });
  });
}
