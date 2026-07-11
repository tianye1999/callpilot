import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const script = readFileSync(join(process.cwd(), "public", "remote_dialer.js"), "utf8");
const page = readFileSync(join(process.cwd(), "public", "index.html"), "utf8");

describe("hosted dialer", () => {
  it("uses the cloud pairing and call resources", () => {
    expect(script).toContain('postJson("/v1/pairing-sessions/claim"');
    expect(script).toContain('postJson("/v1/calls"');
    expect(script).toContain('fetch(`/v1/calls/${encodeURIComponent(payload.callId)}`');
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

  it("allows the configured LiveKit Cloud discovery request", () => {
    expect(page).toContain("connect-src 'self' https://*.livekit.cloud wss:");
  });
});
