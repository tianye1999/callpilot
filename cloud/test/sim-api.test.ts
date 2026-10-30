import { afterEach, describe, expect, it, vi } from "vitest";

import worker, { type Env } from "../src/sim-api";

const endpoint = "https://example.test/v1/releases/latest";
const query = "?product=physical-sim&platform=macos";
const release = {
  product: "physical-sim",
  platform: "macos",
  version: "26.10.1",
  published_at: 1793232000000,
  notes_zh: "Release notes zh",
  notes_en: "Release notes en",
  download_url: "https://downloads.example.test/app.dmg"
};
const payload = {
  product: release.product,
  platform: release.platform,
  version: release.version,
  publishedAt: release.published_at,
  notes: { zh: release.notes_zh, en: release.notes_en },
  downloadUrl: release.download_url
};

function releaseEnv(row: typeof release | null = release) {
  const first = vi.fn().mockResolvedValue(row);
  const bind = vi.fn().mockReturnValue({ first });
  const prepare = vi.fn().mockReturnValue({ bind });
  return { env: { DB: { prepare } } as unknown as Env, prepare, bind };
}

afterEach(() => vi.restoreAllMocks());

describe("GET /v1/releases/latest", () => {
  it("returns 404 with the existing error envelope when no release exists", async () => {
    const { env } = releaseEnv(null);
    const response = await worker.fetch(new Request(endpoint + query), env);
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({
      error: { code: "NOT_FOUND", message: "Release not found", requestId: expect.stringMatching(/^req_/) }
    });
    expect(response.headers.get("Cache-Control")).toBe("no-store");
  });

  it("selects the latest published release for the exact product and platform", async () => {
    const { env, prepare, bind } = releaseEnv();
    const response = await worker.fetch(new Request(endpoint + query), env);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(payload);
    expect(prepare).toHaveBeenCalledOnce();
    expect(prepare).toHaveBeenCalledWith(expect.stringMatching(
      /WHERE product = \?1 AND platform = \?2 ORDER BY published_at DESC.*LIMIT 1/
    ));
    expect(bind).toHaveBeenCalledWith("physical-sim", "macos");
  });

  it("caches successful public responses for 300 seconds", async () => {
    const { env } = releaseEnv();
    const response = await worker.fetch(new Request(endpoint + query), env);
    expect(response.headers.get("Cache-Control")).toBe("public, max-age=300");
    expect(response.headers.get("X-Content-Type-Options")).toBe("nosniff");
    expect(response.headers.has("Set-Cookie")).toBe(false);
  });

  it.each([
    "", "?product=physical-sim", "?platform=macos",
    "?product=&platform=macos", "?product=physical-sim&platform=",
    "?product=Physical-Sim&platform=macos", "?product=physical-sim&platform=mac/os",
    `?product=${"a".repeat(65)}&platform=macos`,
    `?product=physical-sim&platform=${"a".repeat(65)}`,
    `${query}&product=other`, `${query}&platform=windows`, `${query}&extra=value`
  ])("rejects invalid query parameters: %s", async (invalidQuery) => {
    const { env, prepare } = releaseEnv();
    const response = await worker.fetch(new Request(endpoint + invalidQuery), env);
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ error: { code: "VALIDATION_ERROR" } });
    expect(prepare).not.toHaveBeenCalled();
  });

  it.each(["http://example.test/app.dmg", "javascript:alert(1)", "//example.test/app.dmg", "https://", "not a url"])(
    "does not return an invalid or non-HTTPS download URL: %s", async (downloadUrl) => {
      const { env } = releaseEnv({ ...release, download_url: downloadUrl });
      const response = await worker.fetch(new Request(endpoint + query), env);
      expect(response.status).toBe(500);
      expect(await response.json()).toEqual({
        error: {
          code: "INTERNAL_ERROR", message: "The request could not be completed",
          requestId: expect.stringMatching(/^req_/)
        }
      });
      expect(response.headers.get("Cache-Control")).toBe("no-store");
    }
  );

  it.each(["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])(
    "rejects %s without accessing D1", async (method) => {
      const { env, prepare } = releaseEnv();
      const response = await worker.fetch(new Request(endpoint + query, { method }), env);
      expect(response.status).toBe(405);
      expect(response.headers.get("Allow")).toBe("GET");
      expect(await response.json()).toMatchObject({ error: { code: "METHOD_NOT_ALLOWED" } });
      expect(prepare).not.toHaveBeenCalled();
    }
  );

  it("does not read identity headers, echo them, or log request content", async () => {
    const { env, prepare } = releaseEnv();
    const request = new Request(endpoint + query, { headers: {
      Authorization: "Bearer private-credential", Cookie: "private-cookie=value",
      "CF-Connecting-IP": "192.0.2.1", "X-Request-Id": "private-request-id"
    } });
    const readHeader = vi.spyOn(request.headers, "get");
    const log = vi.spyOn(console, "log");
    const error = vi.spyOn(console, "error");
    const response = await worker.fetch(request, env);
    expect(await response.json()).toEqual(payload);
    expect(readHeader).not.toHaveBeenCalled();
    expect(prepare).toHaveBeenCalledOnce();
    expect(log).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
  });
});

describe("SIM API routing", () => {
  it("serves health without accessing D1", async () => {
    const { env, prepare } = releaseEnv();
    const response = await worker.fetch(new Request("https://example.test/healthz"), env);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ ok: true });
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(prepare).not.toHaveBeenCalled();
  });

  it.each(["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])(
    "rejects %s on health with Allow: GET", async (method) => {
      const { env, prepare } = releaseEnv();
      const response = await worker.fetch(new Request("https://example.test/healthz", { method }), env);
      expect(response.status).toBe(405);
      expect(response.headers.get("Allow")).toBe("GET");
      expect(prepare).not.toHaveBeenCalled();
    }
  );

  it.each(["/", "/v1/releases", "/v1/admin/enrollment-invites", "/v1/edges/connect", "/unknown"])(
    "returns JSON 404 for %s without accessing D1", async (path) => {
      const { env, prepare } = releaseEnv();
      const response = await worker.fetch(new Request(`https://example.test${path}`), env);
      expect(response.status).toBe(404);
      expect(await response.json()).toMatchObject({ error: { code: "NOT_FOUND" } });
      expect(response.headers.get("X-Content-Type-Options")).toBe("nosniff");
      expect(prepare).not.toHaveBeenCalled();
    }
  );

  it("does not expose storage errors", async () => {
    const { env, prepare } = releaseEnv();
    prepare.mockImplementation(() => { throw new Error("private database details"); });
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const response = await worker.fetch(new Request(endpoint + query), env);
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({
      error: {
        code: "INTERNAL_ERROR", message: "The request could not be completed",
        requestId: expect.stringMatching(/^req_/)
      }
    });
    expect(JSON.stringify(log.mock.calls)).not.toContain("private database details");
  });
});
