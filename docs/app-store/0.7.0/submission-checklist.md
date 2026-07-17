# App Store submission checklist for 0.7.0

## Build and capability gates

- [ ] PushKit VoIP notification and CallKit system incoming-call flow pass locked-screen,
      terminated-app, duplicate-push, stale-offer and revoke tests on a physical iPhone.
- [ ] App Review reviewer Edge is isolated from real content and restricts toll-risk calls.
- [ ] Three unclaimed, unexpired one-time review codes work and disabling the Worker flag
      invalidates unclaimed codes.
- [ ] Full iOS simulator tests, generic device build, archive validation and export succeed.
- [ ] The archived app contains the app and dependency privacy manifests at expected paths.
- [ ] Build number is unique and `ITSAppUsesNonExemptEncryption=false` remains present.
- [ ] Build is uploaded and App Store Connect processing reaches a completed state.

## App Store Connect fields

- [ ] English and Simplified Chinese metadata from `metadata.md` are entered.
- [ ] Privacy, Support and Marketing URLs return HTTP 200 from a signed-out browser.
- [ ] App Privacy answers match `privacy-answers.md` and are published.
- [ ] Age rating questionnaire reflects calling, messaging, AI content and unrestricted
      access to user-provided communications; no emergency-service claim is made.
- [ ] Content rights, export compliance, advertising identifier and encryption questions
      are answered from the actual binary and service configuration.
- [ ] App Review contact has a direct phone and monitored email.
- [ ] Final review notes contain live codes and no secret other than those bounded codes.
- [ ] Required iPhone screenshots use sanitized content, correct localization and no
      status-bar, notification, message, phone-number or credential leaks.

## Release policy

- [ ] First submit Build 4 to external TestFlight and complete at least one non-owner
      install, pairing, call, content refresh and revoke/recovery pass.
- [ ] Resolve every TestFlight crash, privacy or onboarding blocker before production review.
- [ ] Select manual release for the first App Store version.
- [ ] Keep the reviewer Edge and monitoring online throughout review; revoke all reviewer
      devices and disable review pairing immediately after review finishes.
