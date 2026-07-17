import { describe, expect, it } from "vitest";

import {
  appReviewPairingEnabled,
  validatePairingPolicy
} from "../src/pairing-policy";

describe("pairing policy", () => {
  it("enables review access only for the exact opt-in value", () => {
    expect(appReviewPairingEnabled("true")).toBe(true);
    expect(appReviewPairingEnabled(undefined)).toBe(false);
    expect(appReviewPairingEnabled("TRUE")).toBe(false);
    expect(appReviewPairingEnabled("1")).toBe(false);
  });

  it("keeps standard pairing bounded to ten minutes", () => {
    expect(() => validatePairingPolicy("standard", 600, false)).not.toThrow();
    expect(() => validatePairingPolicy("standard", 601, true)).toThrowError(
      /PAIRING_TTL_INVALID/
    );
  });

  it("allows review pairing for at most seven days when explicitly enabled", () => {
    expect(() => validatePairingPolicy("app_review", 604_800, true)).not.toThrow();
    expect(() => validatePairingPolicy("app_review", 604_801, true)).toThrowError(
      /PAIRING_TTL_INVALID/
    );
    expect(() => validatePairingPolicy("app_review", 3_600, false)).toThrowError(
      /FEATURE_DISABLED/
    );
  });
});
