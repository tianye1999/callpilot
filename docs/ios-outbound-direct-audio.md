# iOS App 外呼直连音频：SoloAmbient 根因与验收证据

> **真机验收（2026-11-03 23:07）**：修复后 iPhone 外呼直连 10086，seq 4–13 全程 PlayAndRecord/VoiceChat + MicrophoneBuiltIn，Edge 60 s 收手机 RTP 3739 包，无 LiveKit 回落，DTMF 3 次成功，双向可听；client_edge_connected 45 s → 12.4 s。

## Task A2: cached RTC configuration writer (2026-11-03)

Issue remains #96; local handoff only. No GitHub comment, installation, call,
commit, push or deployment was performed.

### Binary attribution

Inspected the resolved 144.7559.10 ios-arm64 framework, not a simulator slice:
`LiveKitWebRTC.xcframework/ios-arm64/LiveKitWebRTC.framework/LiveKitWebRTC`.
SHA256: `a8939496df69d9999682f962ac1e0465d326e88bf3f0fe8f33cc2e6934353ee9`.
Addresses below are unslid Mach-O virtual addresses, not device ASLR addresses.

`xcrun llvm-objdump --macho --disassemble --symbolize-operands` and `otool -ov`
establish the following (Objective-C method names mapped through method metadata;
C++ functions identified by embedded function/file log strings and instructions):

| Address | Evidence |
| --- | --- |
| 0x33b2dc | AudioEngineDevice::IsAudioSessionCategoryValid, bool validator |
| 0x33b3c0 | Only annotated SoloAmbient constant load in the disassembly |
| 0x33b3d0 | Uses that value in isEqualToString:, not setCategory |
| 0x1994b4 | -[LKRTCAudioSessionConfiguration init] |
| 0x199510 / 0x199520 | Reads AVAudioSession.category and stores it in config |
| 0x199540 / 0x199550 | Reads AVAudioSession.mode and stores it in config |
| 0x19958c / 0x1995a0 | +initialize creates config and stores global at 0xb24070 |
| 0x199728 / 0x199770 | +webRTCConfiguration / +setWebRTCConfiguration: |
| 0x355a34 | AudioDeviceIOS::ConfigureAudioSession calls configureWebRTCSession: |
| 0x355e28 | ConfigureAudioSessionLocked also calls configureWebRTCSession: |
| 0x1970dc | -[LKRTCAudioSession configureWebRTCSession:] |
| 0x197198 / 0x1971c4 | Reads cached webRTCConfiguration, calls setConfiguration:active:error: |
| 0x192ff4 | -[LKRTCAudioSession(Configuration) setConfiguration:active:shouldSetActive:error:] |
| 0x1931c8 | Calls setCategory:mode:options:error: using configuration fields |
| 0x1955d0 | RTC session wrapper that forwards category/mode to AVAudioSession |

The active writer is the platformDefault ADM's RTC session configuration path,
not an audio-engine ADM selecting SoloAmbient because it has no active tracks.
It writes SoloAmbient/Default when those values are in the cached configuration
and differ from the current session. There is no need for a literal SoloAmbient
write at that call site: the value comes from the cached NSString.

Source cross-check, pinned reference revision of m144_release:
`30d5e63ae91da483e06577b5c35ee91cc5e5c3db` in webrtc-sdk/webrtc:

- sdk/objc/components/audio/RTCAudioSessionConfiguration.m: init / initialize /
  webRTCConfiguration cache the current category/mode, not fixed VoIP defaults.
- sdk/objc/components/audio/RTCAudioSession.mm: setIsAudioEnabled ->
  updateCanPlayOrRecord -> delegate notification; audioSessionDidActivate treats
  external activation as interruption end; configureWebRTCSession reads cache.
- sdk/objc/native/src/audio/audio_device_ios.mm: OnCanPlayOrRecordChange ->
  HandleCanPlayOrRecordChange (629) -> UpdateAudioUnit (896); when initialized
  behind a closed manual gate, opening it initializes the AudioUnit and calls
  ConfigureAudioSession (985). HandleInterruptionEnd also calls UpdateAudioUnit.
  ConfigureAudioSessionLocked (1006) is used by InitPlayOrRecord (1042).
- modules/audio_device/audio_engine_device.mm: OnCanPlayOrRecordChange (617)
  is a log-only no-op; IsAudioSessionCategoryValid (3214) only validates.

Release 144.7559.10 points to webrtc-build release m144.7559.10, but that public
release does not state a source SHA. Do not claim the branch reference is a
byte-identical build provenance. The relevant cache, configuration calls and
SoloAmbient comparison above are independently established in the shipped binary.

The reported 2 ms overwrite matches opening the manual gate: setting the real
AVAudioSession in configureStandaloneSession never updated the RTC cache.
Reinitializing recording/playout during repair reapplies the same stale cache,
explaining the repeat. A cold incoming call configures PlayAndRecord/VoiceChat
in prepareForIncoming *before* first touching AudioManager, so initial RTC config
can capture the correct session. This is order-dependent, not intrinsic CallKit
immunity; a process with a previously initialized stale cache can be vulnerable.
No historical device stack or cached value was collected in this coding task.
The new sync log prints cached and target category/mode to verify this inference
on the next authorized run; if cached is already correct and overwrite persists,
break on AVAudioSession setCategory selectors and capture the actual writer stack.

### Fix and acceptance

WebRtcAudioBridge.activate now publishes a fresh RTC config under the RTC session
lock *before* external activation and gate-open callbacks. It copies the actual
category/mode/options of the session already configured by standalone or CallKit,
and preserves RTC preferred sample rate, buffer duration and channel counts.
It does not copy inactive route channel counts (which may be zero), mutate an
object retained by native readers, or add any AVAudioSession write/polling loop.
Each activation resynchronizes, including subsequent CallKit direct calls, so an
outbound call cannot leave receiver/mixing options pinned for incoming calls.
LiveKit automatic configuration, engine availability, teardown and fallback are
unchanged. Existing diagnostic snapshots and the bounded startup repair remain.

The core regression is a source-order/config-field guard, not an iPhone ADM test.
True acceptance remains Claude's device run: cached SoloAmbient -> target VoIP
log, activate_return and delayed samples stay PlayAndRecord/VoiceChat with mic
input; RTP packets AND source sample duration increase; bidirectional audio;
CallKit incoming and direct-to-LiveKit fallback regressions. Bluetooth and other
external session owners remain untested.

### Latency assessment (report only)

- claim_start 2321 -> response 8505 (6184 ms) wraps createDirectCall, not an
  incoming claim. POST /v1/calls waits for Cloud TURN issuance, Edge relay and
  Edge SDP answer (cloud/src/outbound-direct.ts startOutboundDirect). Edge
  WebRtcRemoteMediaEndpoint.connect awaits warm_webrtc_stack, setRemoteDescription,
  createAnswer and setLocalDescription/ICE gathering. iOS can also retry one
  URLError and poll a pending same-key response. These marks cannot apportion
  network/Cloud/Edge/ICE time, nor prove a retry occurred. Inspect server relay
  timing and Edge endpoint_connect_start/direct_answer_ready plus warm-up log.
- response 8505 -> direct_control_ready 14911 (6406 ms) is a separate peer
  ICE/DTLS/data-channel connection interval, not included in claim duration.
- mic_enable_start 21117 -> permission_ready 28771 -> return 29935: the start
  mark precedes the entire LiveKitRoomTransport.connect, not merely mic publish.
  permission_ready is LiveKit ParticipantPermissions (publish/subscribe/data/
  microphone source), emitted by didUpdatePermissions, NOT iOS mic permission.
  SDK Room.connect concurrently starts a mic track, connects signaling/media,
  then awaits the track and publishes it. Thus 7.654 s precedes receipt/delivery
  of the permission event, followed by 1.164 s to connect return. This suggests
  room/signaling connection dominates, but a delegate/MainActor delay or track
  startup delay needs SDK/network logs to distinguish; no fixed 9 s wait exists.
- edge_connected 45019 is receipt of Edge status connected, not RTC connectivity.
  iOS waits for mediaReady before sending the pending dial packet. Edge then
  dials, waits for modem.is_call_connected, enables voice PCM and starts bridge
  before publishing connected (remote_dialer.py). The last 15.084 s after room
  return can include media readiness, control delivery, ringing/human answer,
  modem/PCM startup and status delivery. Cannot assign it all to iOS or assume
  exactly 15 s ringing without dial_sent / Edge dialing / answered timestamps.

The Task A text below describes the original baseline before this A2 correction.

Scope: iOS only, issue #96. No installation, phone call, commit or deployment.

## What is established

The reported ADM recording/playing flags do not establish that the shared
AVAudioSession is input-capable. The old startup verifier accepted SoloAmbient
with zero input channels whenever those two flags were true. The standalone
verifier now also requires PlayAndRecord, VoiceChat, inputAvailable, at least
one input channel and a nonempty input route. CallKit keeps the original test.

When standalone readback fails, stop recording/playout, reapply category and
activation under the RTC configuration lock, then initialize/start both ADM
sides and read back again (bounded 200 ms). All native return codes are logged.
An unrepaired startup uses the existing failure/fallback behavior. No periodic
restart loop is added. The +300 ms/+1 s/+3 s observations are diagnostics only.

This is a defensive repair of a demonstrated false-positive readiness check,
not proof of the writer of SoloAmbient or proof of restored microphone RTP.
That attribution requires the next authorized device run.

## Source evidence

Resolved dependencies: LiveKit Swift 2.15.1 (7f3af148), WebRTC 144.7559.10.
The local SDK checkout is under .validation/DerivedData/SourcePackages/checkouts.

- client-sdk-swift/Sources/LiveKit/Audio/AudioSessionEngineObserver.swift:
  configureIfNeeded checks isAutomaticConfigurationEnabled;
  configureAudioSession deactivates when requirements become empty, and sets
  playback/playAndRecord when requirements exist. It does not set SoloAmbient.
- client-sdk-swift/Sources/LiveKit/Audio/Manager/AudioManager.swift:
  setEngineAvailability forwards to the LiveKit audioEngine ADM. The direct
  factory explicitly uses a different, platformDefault AudioUnit ADM.
- WebRTC headers shipped in the pinned artifact: RTCAudioSession.h documents
  useManualAudio/isAudioEnabled as the AudioUnit gate, and configuration locking.
- Upstream source inspected as reference (m144_release, not independently
  proven byte-identical to the shipped binary):
  https://github.com/webrtc-sdk/webrtc/blob/m144_release/sdk/objc/components/audio/RTCAudioSession.mm
  configureWebRTCSession applies webRTCConfiguration; unconfigureWebRTCSession
  only balances setActive:NO. canPlayOrRecord uses !useManualAudio || isAudioEnabled.
  https://github.com/webrtc-sdk/webrtc/blob/m144_release/sdk/objc/native/src/audio/audio_device_ios.mm
  InitPlayOrRecord defers AudioUnit configuration while canPlayOrRecord is false;
  UpdateAudioUnit configures/initializes it when the gate opens. recording/playing
  are not a replacement for shared-session category/route readback.
- WebRtcAudioBridge.preparePrivacyGate disables native audio before factory
  creation; activate sends the external activation notification then opens the
  gate; release closes it; didClose restores automatic native gating only if
  no newer direct owner exists. These CallKit operations are unchanged.
- LiveKitAudioDevice.prepare(.standalone) still enables LiveKit automatic
  session configuration and default engine availability. This change never
  disables that management, so fallback retains standalone ownership. Direct
  stop cancels observation/verification and revokes its own audio before fallback.

## Device evidence to inspect

Each diagnostic becomes a separate client_timing batch, with the original
audioEngine/audioSession field allowlists unchanged. Before the control channel
opens, observations are queued (maximum 16); opening it drains each observation
individually. Once open, each observation schedules immediate publication without
the LiveKit 30 ms coalescing delay. No delivery acknowledgement is implied.

In Edge events.jsonl, inspect client_audio_engine_state in ascending seq. The
first five diagnostic snapshots are acquire, prepare, category, activation,
and gate-open readback; a failed API can truncate that sequence. Diagnostic-only
batches use one existing mark:

| Mark | Observation |
| --- | --- |
| audio_lease_end | after standalone acquire, before TURN request/privacy gate |
| preconnect_ready | after prepareDirect, before createDirectCall |
| configure_start | immediately after setCategory returns (success or throw) |
| configure_end | immediately after setActive returns (success or throw) |
| activate_return | immediately after activateDirectAudio returns |
| audio_engine_running | +300 ms, +1 s, +3 s from standalone activation |

The marks contain actual capture times relative to the existing timing origin,
not time of queued delivery. Repair repeats configure_start/configure_end; do
not mistake those repeats for a second activation. Normal milestone batches can
also carry the last verification snapshot. No new wire field or mark was added.
Edge latency events retain only the first occurrence of a mark; they do not
retain repeated diagnostic mark times. Edge audio observations retain seq and
their event timestamps. For exact stage/capture-time attribution (especially
repair repeats), correlate seq with iOS CallTiming audio_diagnostic_seq logs,
which include stage and relative ms. After the first five, normal verification
batches can also contain audioEngine; do not identify delayed samples by ordinal
alone. If teardown/fallback occurs before a delayed
observation, it is canceled rather than sampling the next transport's session.

Read audioEngine.audioSession.category/mode/inputs/inputNumberOfChannels at each
stage. The first mismatch distinguishes failure to apply the category from a
later overwrite. Inputs may still be empty between category and activation.
After activation expect PlayAndRecord, VoiceChat and a real input route/channel.
platformDefault running=false alone is not failure evidence.

In iOS os.Logger subsystem ai.bondings.callpilot.sim, category DirectAudio, read
owner, stage, monotonic capture time, setCategory/setActive success/error,
useManualAudio/isAudioEnabled/isActive, route changes (reason), interruptions
(type/options), mediaServicesWereReset and ADM repair return codes. Only port
types are logged, never device names/UIDs or media content. System notification
readback is scheduled onto MainActor; its route may reflect a subsequent change.

Success still requires client_rtp_stats outPackets increasing AND
source.totalSamplesDuration increasing, audible bidirectional media, no
no_phone_audio fallback, plus a separate CallKit incoming regression and a
direct-to-LiveKit fallback regression. Session flags or build/tests alone are
not device acceptance. Bluetooth route transitions and delayed category
overwrites beyond startup remain device risks.
