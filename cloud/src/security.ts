const textEncoder = new TextEncoder();

export function randomId(prefix: string, bytes = 18): string {
  const value = new Uint8Array(bytes);
  crypto.getRandomValues(value);
  return `${prefix}_${base64Url(value)}`;
}

export function randomSecret(bytes = 32): string {
  const value = new Uint8Array(bytes);
  crypto.getRandomValues(value);
  return base64Url(value);
}

export function randomPairingCode(): string {
  const alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ";
  const values = new Uint8Array(8);
  crypto.getRandomValues(values);
  const raw = Array.from(values, (value) => alphabet[value % alphabet.length]).join("");
  return `${raw.slice(0, 4)}-${raw.slice(4)}`;
}

export async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", textEncoder.encode(value));
  return base64Url(new Uint8Array(digest));
}

export async function constantTimeEqual(left: string, right: string): Promise<boolean> {
  const leftHash = await sha256(left);
  const rightHash = await sha256(right);
  if (leftHash.length !== rightHash.length) return false;
  let different = 0;
  for (let index = 0; index < leftHash.length; index += 1) {
    different |= leftHash.charCodeAt(index) ^ rightHash.charCodeAt(index);
  }
  return different === 0;
}

export function bearerCredential(request: Request): [string, string] | null {
  const header = request.headers.get("Authorization") ?? "";
  if (!header.startsWith("Bearer ") || header.length > 512) return null;
  const value = header.slice(7);
  const separator = value.indexOf(".");
  if (separator < 1) return null;
  const id = value.slice(0, separator);
  const secret = value.slice(separator + 1);
  if (!/^[a-z]+_[A-Za-z0-9_-]{12,80}$/.test(id) || secret.length < 32) return null;
  return [id, secret];
}

export function cookieCredential(request: Request): [string, string] | null {
  const cookie = request.headers.get("Cookie") ?? "";
  for (const part of cookie.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name !== "__Host-callpilot-device") continue;
    const value = rest.join("=");
    if (value.length > 512) return null;
    const separator = value.indexOf(".");
    if (separator < 1) return null;
    return [value.slice(0, separator), value.slice(separator + 1)];
  }
  return null;
}

function base64Url(value: Uint8Array): string {
  let raw = "";
  for (const byte of value) raw += String.fromCharCode(byte);
  return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

