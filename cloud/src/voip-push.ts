import type { Env } from "./types";

const encoder = new TextEncoder();
const decoder = new TextDecoder();

export interface EncryptedPushToken {
  ciphertext: string;
  nonce: string;
}

export interface VoipPushPayload {
  aps: { "content-available": 1 };
  v: 1;
  type: "inbound.offer";
  offerId: string;
  callUUID: string;
  expiresAtUnixMs: number;
}

interface ProviderTokenInput {
  keyId: string;
  teamId: string;
  privateKeyPEM: string;
  nowSeconds?: number;
}

interface PushTarget {
  device_id: string;
  token_ciphertext: string;
  token_nonce: string;
  environment: "sandbox" | "production";
}

interface InboundPushInput {
  edgeId: string;
  offerId: string;
  callUUID: string;
  expiresAtUnixMs: number;
}

type Fetcher = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;

export function voipPushEnabled(value: string | undefined): boolean {
  return value === "true";
}

export function buildVoipPayload(input: Omit<VoipPushPayload, "aps" | "v" | "type">): VoipPushPayload {
  return {
    aps: { "content-available": 1 },
    v: 1,
    type: "inbound.offer",
    ...input
  };
}

export async function encryptPushToken(token: string, rawKey: Uint8Array): Promise<EncryptedPushToken> {
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const key = await importAesKey(rawKey, ["encrypt"]);
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce },
    key,
    encoder.encode(token.toLowerCase())
  );
  return {
    ciphertext: encodeBase64URL(new Uint8Array(ciphertext)),
    nonce: encodeBase64URL(nonce)
  };
}

export async function decryptPushToken(
  encrypted: EncryptedPushToken,
  rawKey: Uint8Array
): Promise<string> {
  const key = await importAesKey(rawKey, ["decrypt"]);
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: decodeBase64URL(encrypted.nonce) },
    key,
    decodeBase64URL(encrypted.ciphertext)
  );
  return decoder.decode(plaintext);
}

export async function createApnsProviderToken(input: ProviderTokenInput): Promise<string> {
  const header = encodeBase64URL(encoder.encode(JSON.stringify({ alg: "ES256", kid: input.keyId })));
  const claims = encodeBase64URL(encoder.encode(JSON.stringify({
    iss: input.teamId,
    iat: input.nowSeconds ?? Math.floor(Date.now() / 1000)
  })));
  const signingInput = `${header}.${claims}`;
  const key = await crypto.subtle.importKey(
    "pkcs8",
    pemBody(input.privateKeyPEM),
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign"]
  );
  const signature = await crypto.subtle.sign(
    { name: "ECDSA", hash: "SHA-256" },
    key,
    encoder.encode(signingInput)
  );
  return `${signingInput}.${encodeBase64URL(new Uint8Array(signature))}`;
}

export async function dispatchInboundOfferPushes(
  env: Env,
  input: InboundPushInput,
  fetcher: Fetcher = fetch
): Promise<void> {
  if (!voipPushEnabled(env.VOIP_PUSH_ENABLED)) return;
  const config = readPushConfig(env);
  const encryptionKey = decodeEncryptionKey(config.encryptionKey);
  const targets = await env.DB.prepare(
    "SELECT push.device_id, push.token_ciphertext, push.token_nonce, push.environment FROM device_push_tokens AS push JOIN devices ON devices.device_id = push.device_id WHERE devices.edge_id = ?1 AND devices.revoked_at IS NULL"
  ).bind(input.edgeId).all<PushTarget>();
  if (!targets.results?.length) return;

  const authorization = await createApnsProviderToken({
    keyId: config.keyId,
    teamId: config.teamId,
    privateKeyPEM: config.privateKey
  });
  const payload = buildVoipPayload({
    offerId: input.offerId,
    callUUID: input.callUUID,
    expiresAtUnixMs: input.expiresAtUnixMs
  });
  await Promise.allSettled(targets.results.map(async (target) => {
    const token = await decryptPushToken({
      ciphertext: target.token_ciphertext,
      nonce: target.token_nonce
    }, encryptionKey);
    const host = target.environment === "production"
      ? "https://api.push.apple.com"
      : "https://api.sandbox.push.apple.com";
    const response = await fetcher(`${host}/3/device/${token}`, {
      method: "POST",
      headers: {
        Authorization: `bearer ${authorization}`,
        "Content-Type": "application/json",
        "apns-expiration": String(Math.floor(input.expiresAtUnixMs / 1000)),
        "apns-priority": "10",
        "apns-push-type": "voip",
        "apns-topic": `${config.bundleId}.voip`
      },
      body: JSON.stringify(payload)
    });
    if (response.ok) return;
    const reason = await apnsReason(response);
    if (response.status === 410 || (response.status === 400 && reason === "BadDeviceToken")) {
      await env.DB.prepare("DELETE FROM device_push_tokens WHERE device_id = ?1")
        .bind(target.device_id).run();
    }
  }));
}

export function decodeEncryptionKey(value: string): Uint8Array {
  const decoded = decodeBase64URL(value);
  if (decoded.byteLength !== 32) throw new Error("PUSH_TOKEN_ENCRYPTION_KEY must decode to 32 bytes");
  return decoded;
}

function readPushConfig(env: Env): {
  teamId: string;
  keyId: string;
  privateKey: string;
  bundleId: string;
  encryptionKey: string;
} {
  const values = {
    teamId: env.APNS_TEAM_ID,
    keyId: env.APNS_KEY_ID,
    privateKey: env.APNS_PRIVATE_KEY,
    bundleId: env.APNS_BUNDLE_ID,
    encryptionKey: env.PUSH_TOKEN_ENCRYPTION_KEY
  };
  if (Object.values(values).some((value) => !value)) {
    throw new Error("VoIP push credentials are incomplete");
  }
  return values as Record<keyof typeof values, string>;
}

async function importAesKey(
  rawKey: Uint8Array,
  usages: Array<"encrypt" | "decrypt">
): Promise<CryptoKey> {
  if (rawKey.byteLength !== 32) throw new Error("Push token encryption key must be 32 bytes");
  return crypto.subtle.importKey("raw", rawKey, "AES-GCM", false, usages);
}

function pemBody(value: string): Uint8Array {
  const normalized = value.replace(/\\n/g, "\n");
  const body = normalized
    .replace(/-----BEGIN PRIVATE KEY-----/g, "")
    .replace(/-----END PRIVATE KEY-----/g, "")
    .replace(/\s/g, "");
  if (!body) throw new Error("APNs private key is invalid");
  return Uint8Array.from(atob(body), (character) => character.charCodeAt(0));
}

function encodeBase64URL(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function decodeBase64URL(value: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("Invalid base64url value");
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
}

async function apnsReason(response: Response): Promise<string | undefined> {
  try {
    const body = await response.json<{ reason?: unknown }>();
    return typeof body.reason === "string" ? body.reason : undefined;
  } catch {
    return undefined;
  }
}
