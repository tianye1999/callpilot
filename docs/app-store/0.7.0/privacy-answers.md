# App Privacy answers for 0.7.0

## App Store Connect questionnaire

Answer **Yes** to data collection. The service retains limited identifiers and audit
metadata even though it does not retain message bodies, transcripts, recordings or
call audio in the cloud content path.

Declare these four data types:

| App Store data type | Linked to user | Tracking | Purpose | Product evidence |
| --- | --- | --- | --- | --- |
| Identifiers / User ID | Yes | No | App Functionality | Opaque Edge ID identifies the paired service account |
| Identifiers / Device ID | Yes | No | App Functionality | Opaque device ID authenticates and revokes one phone |
| Usage Data / Product Interaction | Yes | No | App Functionality | Security audit events record closed actions such as pairing, calls and content reads |
| Diagnostics / Other Diagnostic Data | Yes | No | App Functionality | Connection and stable failure metadata supports delivery, security and recovery |

Do not declare advertising, analytics, product personalization or tracking. The App
contains no advertising or analytics SDK and `NSPrivacyTracking` is false.

## Content handled only to service a live request

SMS bodies, call transcripts, summaries and audio are transmitted off the phone but are
not retained by the CallPilot control plane beyond the time needed to service the live
request. Apple states that data immediately discarded after servicing a request is not
"collected" for App Privacy answers. This boundary must be re-evaluated before adding
cloud recording, content logs, analytics, crash reporting, or server-side content cache.

## Manifest parity

`ios/CallPilot/PrivacyInfo.xcprivacy` declares the same four retained metadata types as
linked, not tracked, and used only for App Functionality. LiveKit and its binary
dependencies supply their own manifests for required-reason API use. The app target does
not add a required-reason API declaration unless its own source begins using one.

Reference: <https://developer.apple.com/app-store/app-privacy-details/>
