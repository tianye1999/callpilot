import { ZodError } from "zod";
import { error, HttpError, json } from "./http";
import { randomId } from "./security";
import { latestReleaseSchema, releaseDownloadUrlSchema } from "./sim-api-schemas";

export interface Env {
  DB: D1Database;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const requestId = randomId("req", 12);
    try {
      return await route(request, env);
    } catch (caught) {
      if (caught instanceof HttpError) {
        return error(caught.code, caught.message, caught.status, requestId, caught.headers);
      }
      if (caught instanceof ZodError) return error("VALIDATION_ERROR", "Request fields are invalid", 400, requestId);
      console.error(JSON.stringify({ requestId, errorType: caught instanceof Error ? caught.name : "unknown" }));
      return error("INTERNAL_ERROR", "The request could not be completed", 500, requestId);
    }
  }
};

async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/$/, "") || "/";
  if (path !== "/healthz" && path !== "/v1/releases/latest") {
    throw new HttpError("NOT_FOUND", "Not found", 404);
  }
  if (request.method !== "GET") {
    throw new HttpError("METHOD_NOT_ALLOWED", "Only GET is allowed", 405, { Allow: "GET" });
  }
  if (path === "/healthz") return json({ ok: true });
  return getLatestRelease(url, env);
}

async function getLatestRelease(url: URL, env: Env): Promise<Response> {
  for (const key of url.searchParams.keys()) {
    if (url.searchParams.getAll(key).length !== 1) {
      throw new HttpError("VALIDATION_ERROR", "Request fields are invalid", 400);
    }
  }
  const input = latestReleaseSchema.parse(Object.fromEntries(url.searchParams));
  const release = await env.DB.prepare(
    "SELECT product, platform, version, published_at, notes_zh, notes_en, download_url FROM app_releases WHERE product = ?1 AND platform = ?2 ORDER BY published_at DESC, version DESC LIMIT 1"
  ).bind(input.product, input.platform).first<{
    product: string;
    platform: string;
    version: string;
    published_at: number;
    notes_zh: string;
    notes_en: string;
    download_url: string;
  }>();
  if (!release) throw new HttpError("NOT_FOUND", "Release not found", 404);
  // Maintainers write rows directly; never expose a malformed download URL.
  if (!releaseDownloadUrlSchema.safeParse(release.download_url).success) {
    throw new HttpError("INTERNAL_ERROR", "The request could not be completed", 500);
  }
  return json({
    product: release.product,
    platform: release.platform,
    version: release.version,
    publishedAt: release.published_at,
    notes: { zh: release.notes_zh, en: release.notes_en },
    downloadUrl: release.download_url
  }, 200, { "Cache-Control": "public, max-age=300" });
}
