export interface Env {
  DB: D1Database;
  EDGE_ROOMS: DurableObjectNamespace;
  ASSETS: Fetcher;
  ADMIN_TOKEN: string;
  LIVEKIT_API_KEY: string;
  LIVEKIT_API_SECRET: string;
  LIVEKIT_URL: string;
  PUBLIC_ORIGIN: string;
  CONTENT_READ_ENABLED?: string;
  CONTENT_RELAY_TIMEOUT_MS?: string;
  CONTENT_READ_DEVICE_LIMIT?: string;
  CONTENT_READ_EDGE_LIMIT?: string;
  APP_REVIEW_PAIRING_ENABLED?: string;
}

export interface EdgeRecord {
  edge_id: string;
  display_name: string;
  public_key: string;
  secret_hash: string;
  created_at: number;
  revoked_at: number | null;
  last_seen_at: number | null;
}

export interface DeviceRecord {
  device_id: string;
  edge_id: string;
  display_name: string;
  secret_hash: string;
  created_at: number;
  last_used_at: number;
  revoked_at: number | null;
}

export interface CallRecord {
  call_id: string;
  edge_id: string;
  device_id: string;
  idempotency_key: string;
  room_name: string;
  phone_identity: string;
  edge_identity: string;
  status: string;
  error_code: string | null;
  created_at: number;
  expires_at: number;
  updated_at: number;
}

export interface InboundOfferRecord {
  offer_id: string;
  edge_id: string;
  call_id: string;
  generation: number;
  nonce: string;
  status: string;
  claim_id: string | null;
  claimed_device_id: string | null;
  room_name: string | null;
  phone_identity: string | null;
  edge_identity: string | null;
  error_code: string | null;
  created_at: number;
  expires_at: number;
  updated_at: number;
}

export interface EdgePresence {
  connected: boolean;
  lastSeenAt?: number;
  modemOnline?: boolean;
  lineBusy?: boolean;
  version?: string;
}
