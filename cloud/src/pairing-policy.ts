export const STANDARD_PAIRING_MAX_SECONDS = 10 * 60;
export const APP_REVIEW_PAIRING_MAX_SECONDS = 7 * 24 * 60 * 60;

export type PairingPurpose = "standard" | "app_review";
export type PairingPolicyErrorCode = "FEATURE_DISABLED" | "PAIRING_TTL_INVALID";

export class PairingPolicyError extends Error {
  constructor(readonly code: PairingPolicyErrorCode) {
    super(code);
    this.name = "PairingPolicyError";
  }
}

export function appReviewPairingEnabled(value: string | undefined): boolean {
  return value === "true";
}

export function validatePairingPolicy(
  purpose: PairingPurpose,
  ttlSeconds: number,
  reviewEnabled: boolean
): void {
  const maxSeconds = purpose === "app_review"
    ? APP_REVIEW_PAIRING_MAX_SECONDS
    : STANDARD_PAIRING_MAX_SECONDS;
  if (ttlSeconds > maxSeconds) throw new PairingPolicyError("PAIRING_TTL_INVALID");
  if (purpose === "app_review" && !reviewEnabled) {
    throw new PairingPolicyError("FEATURE_DISABLED");
  }
}
