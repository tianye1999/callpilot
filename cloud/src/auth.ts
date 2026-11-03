import { HttpError } from "./http";
import { bearerCredential, cookieCredential, sha256 } from "./security";
import type { DeviceRecord, EdgeRecord, Env } from "./types";

export async function authenticateEdge(request: Request, env: Env): Promise<EdgeRecord> {
  const credential = bearerCredential(request);
  if (!credential || !credential[0].startsWith("edge_")) throw unauthorized();
  const [edgeId, secret] = credential;
  const edge = await env.DB.prepare(
    "SELECT * FROM edges WHERE edge_id = ?1 AND revoked_at IS NULL"
  ).bind(edgeId).first<EdgeRecord>();
  if (!edge || edge.secret_hash !== await sha256(secret)) throw unauthorized();
  if (!await verifyDeviceProof(request, edge)) throw unauthorized();
  return edge;
}

export async function authenticateDevice(request: Request, env: Env, ctx?: ExecutionContext): Promise<DeviceRecord> {
  return (await authenticateDeviceWithReads(request, env, undefined, ctx)).device;
}

/** Additional read-only queries share the fresh credential lookup's transaction.
 * Results must not be consumed until credential validation has succeeded. */
export async function authenticateDeviceWithReads(
  request: Request, env: Env,
  additionalReads?: (deviceId: string) => D1PreparedStatement[],
  ctx?: ExecutionContext
): Promise<{ device: DeviceRecord; reads: D1Result[] }> {
  const credential = cookieCredential(request);
  if (!credential || !credential[0].startsWith("device_")) throw unauthorized();
  const [deviceId, secret] = credential;
  const lookup = env.DB.prepare(
    "SELECT * FROM devices WHERE device_id = ?1 AND revoked_at IS NULL"
  ).bind(deviceId);
  const rows = additionalReads ? await env.DB.batch([lookup, ...additionalReads(deviceId)]) : undefined;
  const device = rows ? rows[0]!.results[0] as DeviceRecord | undefined : await lookup.first<DeviceRecord>();
  if (!device || device.secret_hash !== await sha256(secret)) throw unauthorized();
  const now = Date.now();
  const cutoff = now - 10 * 60_000;
  if (device.last_used_at === null || device.last_used_at <= cutoff) {
    const update = env.DB.prepare(
        "UPDATE devices SET last_used_at = ?1 WHERE device_id = ?2 AND (last_used_at IS NULL OR last_used_at <= ?3)"
      ).bind(now, deviceId, cutoff).run().catch(() => {
      console.warn("Device last-used update failed");
    });
    if (ctx) ctx.waitUntil(update);
    else await update;
  }
  return { device, reads: rows?.slice(1) ?? [] };
}

function unauthorized(): HttpError {
  return new HttpError("UNAUTHORIZED", "Credential is missing, invalid, or revoked", 401);
}

async function verifyDeviceProof(request: Request, edge: EdgeRecord): Promise<boolean> {
  const timestamp = request.headers.get("X-CallPilot-Timestamp") ?? "";
  const signature = request.headers.get("X-CallPilot-Signature") ?? "";
  if (!/^\d{13}$/.test(timestamp) || Math.abs(Date.now() - Number(timestamp)) > 60_000) return false;
  if (!/^[A-Za-z0-9_-]{80,100}$/.test(signature)) return false;
  try {
    const publicKey = await crypto.subtle.importKey(
      "raw",
      base64UrlDecode(edge.public_key),
      { name: "Ed25519" },
      false,
      ["verify"]
    );
    const path = new URL(request.url).pathname;
    const message = new TextEncoder().encode(
      `${edge.edge_id}\n${timestamp}\n${request.method.toUpperCase()}\n${path}`
    );
    return crypto.subtle.verify(
      "Ed25519",
      publicKey,
      base64UrlDecode(signature),
      message
    );
  } catch {
    return false;
  }
}

function base64UrlDecode(value: string): Uint8Array<ArrayBuffer> {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  const raw = atob(padded);
  const output = new Uint8Array(raw.length);
  for (let index = 0; index < raw.length; index += 1) output[index] = raw.charCodeAt(index);
  return output;
}
