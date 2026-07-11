const SECURITY_HEADERS: Record<string, string> = {
  "Cache-Control": "no-store",
  "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY"
};

export function json(data: unknown, status = 200, headers?: HeadersInit): Response {
  const merged = new Headers(SECURITY_HEADERS);
  merged.set("Content-Type", "application/json; charset=utf-8");
  if (headers) new Headers(headers).forEach((value, key) => merged.set(key, value));
  return new Response(JSON.stringify(data), { status, headers: merged });
}

export function error(code: string, message: string, status: number, requestId: string): Response {
  return json({ error: { code, message, requestId } }, status);
}

export async function readJson(request: Request): Promise<unknown> {
  const contentLength = Number(request.headers.get("Content-Length") ?? "0");
  if (contentLength > 16 * 1024) throw new HttpError("PAYLOAD_TOO_LARGE", "Request is too large", 413);
  try {
    return await request.json();
  } catch {
    throw new HttpError("INVALID_JSON", "Request body must be JSON", 400);
  }
}

export function requireSameOrigin(request: Request, publicOrigin: string): void {
  const origin = request.headers.get("Origin");
  if (!origin || origin !== publicOrigin) {
    throw new HttpError("UNTRUSTED_ORIGIN", "Request origin is not allowed", 403);
  }
}

export class HttpError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly status: number
  ) {
    super(message);
  }
}

