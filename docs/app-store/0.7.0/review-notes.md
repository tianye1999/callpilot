# App Review Notes template for 0.7.0

Replace every bracketed field immediately before submission. Never commit pairing codes,
credentials, phone numbers, API keys or reviewer contact details to this file.

## Notes

ATI CallPilot is a companion app for a physical CallPilot Edge. The Edge is a computer
connected to a supported cellular modem and the user's own SIM. For review, we maintain a
dedicated online Edge containing only sanitized test messages and call history. It has no
real user content.

There is no username/password account. On the first screen, enter one of these independent
one-time review pairing codes:

- `[CODE 1]` (expires `[UTC DATE]`)
- `[CODE 2]` (expires `[UTC DATE]`)
- `[CODE 3]` (expires `[UTC DATE]`)

Review service URL: `https://dial-beta.bondings.ai/`

Suggested review flow:

1. Launch the app, enter one unused code, and tap Pair.
2. Confirm the status in Settings shows the reviewer Edge and modem online.
3. Open Messages and Calls to inspect sanitized fixtures, including one AI call summary
   and transcript. Pull to refresh to exercise the live Edge relay.
4. In Dial, enter `10086`, the SIM carrier's automated service line, and place a call.
   Test mute, speaker, keypad, and hang up. Please do not dial emergency or arbitrary
   destinations; the reviewer Edge is restricted to toll-safe test use.
5. `[AFTER PUSHKIT/CALLKIT VERIFICATION: describe the exact safe inbound-call procedure.]`
6. In Settings, clear local content. Unpairing removes the review device credential and
   requires another unused code.

Important limitations and disclosures:

- The App is not a carrier and cannot place emergency calls.
- Calls use the review Edge's SIM and require the Edge to remain online.
- Recording is disabled for the reviewer environment.
- SMS bodies, call content, and audio are not stored by the CallPilot cloud control plane.
- If a code has already been consumed, use the next code or contact `[REVIEW CONTACT]`.

Review contact: `[NAME, DIRECT PHONE, EMAIL]`
