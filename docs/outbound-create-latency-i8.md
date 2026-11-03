# I8: outbound create latency handoff

Round 1 scope: source changes and offline verification only. Claude owns independent
review, relay deployment and physical-device acceptance. No Worker configuration,
schema, Edge Python implementation or media state-machine change is required.

## Round 2 (current implementation)

Input evidence supplied by Claude: sim-relay ad0dbbc9, Android 0.7.7, 2026-11-04
01:06. D1 stages take roughly 180ms each, outbound_create total 3095ms, Edge
answer-ready 1352ms. These are supplied device results, not another Codex dial.

- Batch idempotency and rate reads into `call_checks`, concurrent with presence.
  A replay or rate-limit failure does not await speculative presence; presence
  rejection is captured so it cannot change error precedence or become unhandled.
- Keep INSERT OR IGNORE and the conflict re-read unchanged. Concurrent creates
  with the same key still deliver only one command and return the same callId.
- After accepted/rejected answers, use one transaction batch for the conditional
  update and current-row read (`call_finalize_read`). This also returns the row
  when another writer has already changed pending to a terminal state; unlike a
  naive UPDATE RETURNING-only implementation, zero updated rows is not an error.
- Queue audit with ctx.waitUntil; failures log only outbound_audit_failed/callId.
  An entry point without ctx still awaits the sanitized operation. Audit is now
  best effort by explicit round-2 request and is no longer a response timing stage.
- Expected successful-path saving at the supplied latency: about 184ms from the
  combined checks, 183ms from finalize/read and 186ms from background audit, or
  approximately 0.55s total. With Edge/network unchanged, 3095ms could become
  approximately 2540ms. This is an estimate, not measured acceptance.

Android regression: page-keyed LaunchedEffect cancellation propagates through
withContext(IO), drops the completed result and releases the old loading mutex.
The next page can issue another request even within two seconds. A focused test
reproduced this on the round-1 implementation (4 tests, 1 failed). The cache now
owns a process-scope Deferred independent of any individual UI waiter, retains
the in-flight request across page cancellation and returns cached results to
subsequent callers. Pairing generation fencing and 60s cache retention remain.
iOS already owns an unstructured shared Task; its enhanced test covers three
concurrent callers, one cancelled waiter and a subsequent cache hit, all with
one HTTP request. No iOS production source change was needed in round 2.

Edge TURN allocation already starts as a task before host binding / STUN route
DNS. The gather nevertheless awaits all candidate tasks, up to 1.5s. New local
INFO logs distinguish host/srflx/relay ready, unavailable and cancelled outcomes:

- `Direct ICE candidate`: connection/component/type, ready_ms from gather start,
  operation_ms from that candidate task's start; stun_route covers STUN DNS/route.
- `Direct ICE gather`: total_ms and completed host/srflx/relay counts.
- `Direct answer`: peer ID, remote_description/create_answer/local_description_gather
  durations. `Direct ICE peer=... connection=...` correlates the two log families.
- No IP, ICE credential, SDP, number or exception payload is added to these logs.

There is not yet evidence that TURN caused the 1352ms. If relay becomes ready
near that time it is the likely gather bottleneck; if relay is early but srflx
is cancelled near 1500ms, STUN/route is holding the answer. No gather budget or
candidate selection rule was shortened: allocation-ready does not prove the
relay path works, and losing srflx/late relay could harm cellular connectivity.
A future early-answer policy needs dual-stack/cellular failure testing and, if
late candidates must be retained, a supported trickle signaling path. This round
intentionally adds evidence only rather than trading connectivity for latency.

Round-2 verification:

- Cloud Vitest: 16 files, 339 tests passed. npm run typecheck / npm run lint exit 0.
- Edge full pytest: 4146 passed, 3 skipped, 1800 warnings in 408.82s (0:06:48).
  Final touched-module recheck: 30 passed in 5.02s.
- ruff: All checks passed! Both mypy and mypy --platform win32:
  Success: no issues found in 84 source files.
- Android full unit/lint/assemble: BUILD SUCCESSFUL in 5m 12s;
  53 actionable tasks: 15 executed, 38 up-to-date. Unit report: 247 tests,
  zero failures/ignored. Lint: 0 errors, 53 warnings.
- iOS core: CORE TESTS: 316 executed, 0 failures. Release iphoneos unsigned
  incremental xcodebuild (-quiet, CODE_SIGNING_ALLOWED=NO) exit 0.
- Full npm test integration legs remain reserved for Claude because they invoke
  wrangler migrations and local service operations prohibited in this task.

The historical round-1 walkthrough/results below are retained for comparison.

## Serial path before this change

Phone: audio admission -> POST /v1/calls/ice -> complete local ICE offer ->
POST /v1/calls -> apply answer. Prefetch removes only the phone ICE HTTP request;
it does not pre-create a peer connection or collect candidates.

POST /v1/calls/ice:

1. Same-origin check; D1 device lookup, secret digest validation, conditional
   last_used_at write (at most once per 10 minutes; awaited without ctx).
2. Read/validate body and paired Edge, feature/TURN configuration checks.
3. EdgeRoom /presence fetch, DO storage.get("presence"), current-socket selection,
   response decode and capability check.
4. Cloudflare TURN credential generation HTTP request and decode (10s bound).

POST /v1/calls with WebRTC offer:

1. Same-origin; auth D1 lookup / digest / conditional last-used write.
2. Read/validate body and paired Edge.
3. D1 idempotency lookup. Replay returns immediately; no new offer/answer.
4. D1 hourly call-count lookup, then EdgeRoom /presence fetch (including its
   storage.get("presence")) and response decode.
5. Check rate limit, connected, modem online, line busy in that order.
6. D1 INSERT OR IGNORE. Only a conflicting insertion needs a race re-read.
7. Sign LiveKit Edge JWT locally (fallback session is retained).
8. Generate a separate set of TURN credentials for Edge via upstream HTTP.
   On failure, clear direct_pending in D1 and use the existing plain command.
9. EdgeRoom /call-media-relay fetch: read/validate payload, select current
   WebSocket, install correlated pending answer and timeout, send command.
   No D1 query or TURN generation occurs inside relayCallMedia/acceptCallAnswer.
10. Wait for Edge call.answer. Socket/call/command/deadline/SDP checks remain.
    This interval includes DO <-> Edge network plus Edge setup, not just the
    approximately 0.3s reported by edge_direct_answer_ready.
11. Decode answer; D1 conditional ready/failed update; D1 call re-read for
    current status; awaited audit INSERT; response serialization.

Error and same-call LiveKit fallback writes are retained and timed. The final
call read is intentionally retained because concurrent Edge status updates can
have changed the row. Auth, audit and idempotency semantics are not relaxed.

## Optimization and expected savings

- Step 4's independent rate read and presence run concurrently. Decision order
  remains unchanged even if presence throws. Expected successful-path saving:
  approximately min(rate_read, presence), not a measured fixed millisecond value.
- Phone ICE prefetch: iOS paired root/foreground and successful direct-capability
  refresh; Android paired tab shell/route appearance and RESUMED lifecycle.
  Dialing uses the same cache path, including an in-flight prefetch when possible.
  A completed hit removes one phone ICE HTTP request, approximately the supplied
  1.1-1.2s baseline, not candidate gathering or Edge TURN generation.
- TURN credentials are time-limited, not call-bound or consumed once: generation
  sends only ttl=86520; /v1/calls/ice has no callId. Caches are memory-only, scoped
  to client/pairing and Edge. Unpair clears them; stale in-flight results cannot
  refill the cache. Failures are not cached. Missing expiry is not cached.
- Expiry safety margin is 60s. Cache retention is also capped at 60s because the
  server TTL only adds 120s to the supported 24h call maximum; near-24h reuse would
  reduce long-call lifetime. This deliberately trades hit rate for correctness.
- No server-global TURN cache or cross-device credential reuse was added.

## One-call deployment evidence

Capture POST /v1/calls response `Server-Timing`, or the structured JSON event
`outbound_create` (requestId, callId, HTTP status, milliseconds). ICE requests emit
`outbound_ice`. Stages: auth, request_parse, idempotency, rate_read, presence,
call_insert, edge_token, turn, sdp_relay, answer_parse, call_finalize, call_read,
audit, total. Plain fallback additionally uses edge_send. Errors also get timing.

The DO emits `outbound_edge_relay` with callId/commandId and edge_wait/do_total.
It passes the durations back internally via X-CallPilot-edge_wait and
X-CallPilot-do_total; the outer response includes them in Server-Timing.
No SDP, ICE server URLs, TURN secrets, numbers or request bodies are logged.

Interpretation (nested/overlapping stages must NOT be added twice):

- turn: relay's separate Edge TURN API cost.
- sdp_relay - do_total: approximate Worker <-> DO dispatch/return overhead.
- edge_wait: WebSocket round trip and Edge handling; compare to Edge answer-ready
  duration to identify network/scheduling overhead. Not a pure network metric.
- client claim_response - claim_start - total: approximate client/Worker network,
  upload/download and scheduling overhead; clocks are durations, not synchronized
  timestamps. This cannot isolate radio/TLS/upload independently.
- rate_read and presence overlap. total includes both once on the critical path.
- A warm phone ICE hit should make prepare_start -> prepare_response almost
  immediate and produce no new outbound_ice request for that dial.

## Acceptance gates retained for Claude

- Independent code review; full npm test integration legs; Android/iPhone Wi-Fi
  warm hit, cold miss, expired entry, unpair during fetch, offline/busy/rate-limit,
  idempotent replay and same-call fallback on real devices.
- Compare the supplied baselines to at least one fresh per-stage trace; no claim
  that the unexplained 2.4s (or iPhone 6.2s total) is already eliminated.
- npm test runs Vitest then integration.mjs and sim-api-integration.mjs. Both
  integration scripts invoke local wrangler migrations/dev workers and the latter
  uses shared sim-api configuration. They were intentionally not invoked by Codex
  under the explicit no-migrations/no-service-operation boundary.
- Edge Python is unchanged: pytest/ruff/mypy are not required by this source diff.

## Offline verification

- Cloud: npm run typecheck and npm run lint both exited 0; npx vitest run:
  `Test Files 16 passed (16)`, `Tests 335 passed (335)`.
- iOS: `python3 run-core-tests.py`:
  `CORE TESTS: 316 executed, 0 failures`.
- iOS: Release, generic iphoneos, CODE_SIGNING_ALLOWED=NO:
  `** BUILD SUCCEEDED **`; final-source incremental recheck also exited 0.
- Android final source: :app:testDebugUnitTest, :app:lintDebug and
  :app:assembleDebug: `BUILD SUCCESSFUL in 3m 23s`,
  `53 actionable tasks: 15 executed, 38 up-to-date`.
  Unit report: 246 tests, zero failures/ignored. Lint: 0 errors, 53 warnings.
- Existing warnings were observed (Swift weak-variable warning, Android
  deprecations/coroutine opt-ins and lint warnings); these were not refactored.
