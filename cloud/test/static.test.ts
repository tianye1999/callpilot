import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const script = readFileSync(join(process.cwd(), "public", "remote_dialer.js"), "utf8");
const page = readFileSync(join(process.cwd(), "public", "index.html"), "utf8");
const indexSource = readFileSync(join(process.cwd(), "src", "index.ts"), "utf8");
const serviceWorker = readFileSync(join(process.cwd(), "public", "remote_dialer_sw.js"), "utf8");

describe("hosted dialer", () => {
  it("uses the cloud pairing and call resources", () => {
    expect(script).toContain('postJson("/v1/pairing-sessions/claim"');
    expect(script).toContain('postJson("/v1/calls"');
    expect(script).toContain('fetch(`/v1/calls/${encodeURIComponent(payload.callId)}`');
  });

  it("reads the paired device name via the camelCase field the /v1 API returns", () => {
    // The dialer reads device.displayName; reading the snake_case DB column left
    // the paired-device label always blank. Lock both sides so neither drifts.
    expect(script).toContain("device.displayName");
    expect(script).not.toContain("device.display_name");
    // Producer side: getDevice maps the snake_case DB column to the camelCase API key.
    expect(indexSource).toContain("displayName: device.display_name");
  });

  it("does not render server strings through HTML injection sinks", () => {
    expect(script).not.toMatch(/\.innerHTML\s*=/);
    expect(script).not.toContain("insertAdjacentHTML");
    expect(script).not.toContain("document.write");
  });

  it("keeps authentication out of browser storage", () => {
    expect(script).not.toContain("localStorage");
    expect(script).not.toContain("sessionStorage");
    expect(script).not.toContain("Authorization");
  });

  it("accepts pairing fragments but not legacy direct LiveKit invites", () => {
    expect(script).toContain('fragment.startsWith("pair=")');
    expect(script).not.toContain("parseInviteFragment");
    expect(script).not.toContain("parseInviteUrl");
    expect(script).not.toContain("atob(");
  });

  it("only caches the shell and keeps API responses out of the service worker cache", () => {
    expect(serviceWorker).toContain('url.pathname.startsWith("/api/")');
    expect(serviceWorker).toContain('url.pathname.startsWith("/v1/")');
    expect(serviceWorker).toContain('if (!SHELL.includes(`${url.pathname}${url.search}`)) return;');
  });

  it("leaves the Worker response header as the CSP source of truth", () => {
    expect(page).not.toContain('http-equiv="Content-Security-Policy"');
  });
});
