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
  return edge;
}

export async function authenticateDevice(request: Request, env: Env): Promise<DeviceRecord> {
  const credential = cookieCredential(request);
  if (!credential || !credential[0].startsWith("device_")) throw unauthorized();
  const [deviceId, secret] = credential;
  const device = await env.DB.prepare(
    "SELECT * FROM devices WHERE device_id = ?1 AND revoked_at IS NULL"
  ).bind(deviceId).first<DeviceRecord>();
  if (!device || device.secret_hash !== await sha256(secret)) throw unauthorized();
  await env.DB.prepare("UPDATE devices SET last_used_at = ?1 WHERE device_id = ?2")
    .bind(Date.now(), deviceId).run();
  return device;
}

function unauthorized(): HttpError {
  return new HttpError("UNAUTHORIZED", "Credential is missing, invalid, or revoked", 401);
}

