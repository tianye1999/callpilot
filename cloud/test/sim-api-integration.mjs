import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const cloudDir = fileURLToPath(new URL("..", import.meta.url));
const wrangler = join(cloudDir, "node_modules", ".bin", process.platform === "win32" ? "wrangler.cmd" : "wrangler");
const port = 18788;
const base = `http://127.0.0.1:${port}`;
const persistDir = mkdtempSync(join(tmpdir(), "callpilot-sim-api-"));
const localArgs = ["-c", "wrangler.sim-api.jsonc", "--local", "--persist-to", persistDir];
const releaseProduct = "physical-sim-integration";
let worker;
let workerOutput = "";

try {
  runWrangler(["d1", "migrations", "apply", "DB", ...localArgs]);

  // Complete all synchronous D1 writes before opening HTTP connections. Blocking
  // socket-close processing with spawnSync can leave fetch with stale keep-alive connections.
  d1Rows(`INSERT INTO app_releases
    (product, platform, version, published_at, notes_zh, notes_en, download_url, created_at) VALUES
    ('${releaseProduct}', 'macos', '9.0.0', 1000, 'Older zh', 'Older en', 'https://example.test/old.dmg', 1000),
    ('${releaseProduct}', 'macos', '0.9.0', 2000, 'Tied zh', 'Tied en', 'https://example.test/tied.dmg', 2000),
    ('${releaseProduct}', 'macos', '1.0.0', 2000, 'Latest zh', 'Latest en', 'https://example.test/latest.dmg', 2000),
    ('${releaseProduct}', 'windows', '2.0.0', 3000, '', '', 'https://example.test/app.exe', 3000),
    ('${releaseProduct}-other', 'macos', '3.0.0', 4000, '', '', 'https://example.test/other.dmg', 4000),
    ('${releaseProduct}-invalid', 'macos', '1.0.0', 2000, '', '', 'https://?', 2000)`);
  assert.throws(() => d1Rows(`INSERT INTO app_releases
    (product, platform, version, published_at, notes_zh, notes_en, download_url, created_at)
    VALUES ('${releaseProduct}', 'macos', '1.0.0', 5000, '', '', 'https://example.test/duplicate.dmg', 5000)`),
  /UNIQUE constraint failed/);
  assert.throws(() => d1Rows(`INSERT INTO app_releases
    (product, platform, version, published_at, notes_zh, notes_en, download_url, created_at)
    VALUES ('${releaseProduct}', 'macos', 'insecure', 5000, '', '', 'http://example.test/app.dmg', 5000)`),
  /CHECK constraint failed/);

  worker = spawn(wrangler, ["dev", ...localArgs, "--ip", "127.0.0.1", "--port", String(port)], {
    cwd: cloudDir, stdio: ["ignore", "pipe", "pipe"]
  });
  worker.stdout.on("data", (chunk) => { workerOutput += chunk; });
  worker.stderr.on("data", (chunk) => { workerOutput += chunk; });
  await waitForHealth();

  const missingRelease = await fetch(`${base}/v1/releases/latest?product=${releaseProduct}-missing&platform=macos`);
  assert.equal(missingRelease.status, 404);
  assert.equal((await missingRelease.json()).error.code, "NOT_FOUND");
  const releasePath = `/v1/releases/latest?product=${releaseProduct}&platform=macos`;
  const latestRelease = await fetch(base + releasePath);
  assert.equal(latestRelease.status, 200);
  assert.equal(latestRelease.headers.get("cache-control"), "public, max-age=300");
  assert.equal(latestRelease.headers.get("x-content-type-options"), "nosniff");
  assert.deepEqual(await latestRelease.json(), {
    product: releaseProduct,
    platform: "macos",
    version: "1.0.0",
    publishedAt: 2000,
    notes: { zh: "Latest zh", en: "Latest en" },
    downloadUrl: "https://example.test/latest.dmg"
  });

  for (const path of ["/v1/releases/latest", `${releasePath}&product=other`, `${releasePath}&extra=value`]) {
    const response = await fetch(base + path);
    assert.equal(response.status, 400);
    assert.equal((await response.json()).error.code, "VALIDATION_ERROR");
  }
  const nonGet = await fetch(base + releasePath, { method: "POST" });
  assert.equal(nonGet.status, 405);
  assert.equal(nonGet.headers.get("allow"), "GET");
  assert.equal((await nonGet.json()).error.code, "METHOD_NOT_ALLOWED");
  const unknown = await fetch(`${base}/v1/admin/enrollment-invites`);
  assert.equal(unknown.status, 404);
  assert.equal((await unknown.json()).error.code, "NOT_FOUND");
  const invalidRelease = await fetch(`${base}/v1/releases/latest?product=${releaseProduct}-invalid&platform=macos`);
  assert.equal(invalidRelease.status, 500);
  assert.equal(invalidRelease.headers.get("cache-control"), "no-store");
  const invalidBody = await invalidRelease.json();
  assert.match(invalidBody.error.requestId, /^req_/);
  assert.deepEqual(invalidBody, {
    error: {
      code: "INTERNAL_ERROR", message: "The request could not be completed",
      requestId: invalidBody.error.requestId
    }
  });
  console.log("sim-api integration: passed");
} catch (error) {
  console.error(workerOutput);
  throw error;
} finally {
  if (worker) await stopWorker(worker);
  rmSync(persistDir, { recursive: true, force: true });
}

function runWrangler(args) {
  const result = spawnSync(wrangler, args, { cwd: cloudDir, encoding: "utf8" });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(result.stderr || result.stdout);
  return result.stdout;
}

function d1Rows(command) {
  const payload = JSON.parse(runWrangler(["d1", "execute", "DB", ...localArgs, "--json", "--command", command]));
  return payload[0]?.results ?? [];
}

async function waitForHealth() {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (worker.exitCode !== null) throw new Error(`Worker exited: ${worker.exitCode}`);
    try {
      const response = await fetch(`${base}/healthz`, { signal: AbortSignal.timeout(1000) });
      if (response.ok && (await response.json()).ok === true) return;
    } catch {}
    await delay(200);
  }
  throw new Error("SIM API Worker did not become healthy");
}

async function stopWorker(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const closed = new Promise((resolve) => child.once("close", resolve));
  child.kill("SIGTERM");
  await Promise.race([closed, delay(3000)]);
  if (child.exitCode === null && child.signalCode === null) {
    child.kill("SIGKILL");
    await closed;
  }
}
