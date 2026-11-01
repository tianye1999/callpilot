import Foundation
import XCTest
@testable import CallPilot

@MainActor
final class CallKitAnswerControllerTests: XCTestCase {
    func testMissingActivationAfterTimeoutDoesNotBlockAudioForever() async throws {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let id = UUID()
        controller.begin(callID: id, action: makeAction(events))
        try controller.claimSucceeded(callID: id)
        controller.activationTimedOut(callID: id)
        controller.end(callID: id)
        XCTAssertTrue(controller.hasUnresolvedAudioEvents)
        events.now = events.now.addingTimeInterval(4)
        XCTAssertFalse(controller.hasUnresolvedAudioEvents)
    }

    func testActivationTimeoutAllowsWaitingOutboundAndNextIncomingAnswerAfterGrace() async throws {
        let events = AnswerControllerEvents()
        let clock = AudioDrainTestClock()
        let controller = makeController(events, clock: clock)
        defer { controller.reset() }
        let old = UUID()
        var stopped = 0
        controller.begin(callID: old, action: makeAction(events))
        try controller.claimSucceeded(callID: old, stopAudio: { stopped += 1 })
        controller.activationTimedOut(callID: old)
        controller.end(callID: old)
        var outboundReady: Bool?
        let outbound = Task { outboundReady = await controller.waitUntilAudioDrained() }
        await settleTasks()
        XCTAssertNil(outboundReady)
        clock.advance(1.9)
        await settleTasks()
        XCTAssertNil(outboundReady)
        clock.advance(0.1)
        await settleTasks()
        await outbound.value
        XCTAssertEqual(outboundReady, true)
        XCTAssertEqual(stopped, 1)

        let next = UUID()
        controller.begin(callID: next, action: makeAction(events))
        let incomingReady = await controller.waitUntilAudioDrained(answerID: next)
        XCTAssertTrue(incomingReady)
        try controller.claimSucceeded(callID: next)
        controller.didActivate()
        XCTAssertEqual(events.activated, [next])
    }

    func testEndBeforeActivationExpiresWithoutAnySystemCallback() async throws {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let id = UUID()
        controller.begin(callID: id, action: makeAction(events))
        try controller.claimSucceeded(callID: id)
        controller.end(callID: id)
        events.now += 1.99
        XCTAssertTrue(controller.hasUnresolvedAudioEvents)
        events.now += 0.01
        XCTAssertFalse(controller.hasUnresolvedAudioEvents)
    }

    func testMissingDeactivationStopsOldAudioAtThreeSecondsBeforeAdmission() async throws {
        let events = AnswerControllerEvents()
        let clock = AudioDrainTestClock()
        let controller = makeController(events, clock: clock)
        defer { controller.reset() }
        let old = UUID()
        var stopped = false
        controller.begin(callID: old, action: makeAction(events))
        try controller.claimSucceeded(callID: old, stopAudio: { stopped = true })
        controller.didActivate()
        controller.end(callID: old)
        let next = UUID()
        controller.begin(callID: next, action: makeAction(events))
        var ready: Bool?
        let waiter = Task {
            ready = await controller.waitUntilAudioDrained(answerID: next)
            XCTAssertTrue(stopped)
        }
        await settleTasks()
        clock.advance(2.9)
        await settleTasks()
        XCTAssertNil(ready)
        XCTAssertFalse(stopped)
        clock.advance(0.1)
        await settleTasks()
        await waiter.value
        XCTAssertEqual(ready, true)
        try controller.claimSucceeded(callID: next)
        controller.didActivate()
        XCTAssertEqual(events.activated, [old, next])
    }

    func testLateActivationAndDeactivationDuringGraceAreDiscardedBeforeWaitingAnswerStarts() async throws {
        let events = AnswerControllerEvents()
        let clock = AudioDrainTestClock()
        let controller = makeController(events, clock: clock)
        defer { controller.reset() }
        let old = UUID()
        let next = UUID()
        controller.begin(callID: old, action: makeAction(events))
        try controller.claimSucceeded(callID: old)
        controller.end(callID: old)
        controller.begin(callID: next, action: makeAction(events))
        var ready: Bool?
        let waiter = Task { ready = await controller.waitUntilAudioDrained(answerID: next) }
        await settleTasks()
        clock.advance(1)
        controller.didActivate()
        await settleTasks()
        XCTAssertTrue(events.activated.isEmpty)
        XCTAssertNil(ready)
        clock.advance(1)
        await settleTasks()
        XCTAssertNil(ready)
        controller.didDeactivate()
        clock.advance(0.02)
        await settleTasks()
        await waiter.value
        XCTAssertEqual(ready, true)
        XCTAssertTrue(events.deactivated.isEmpty)
        try controller.claimSucceeded(callID: next)
        controller.didActivate()
        XCTAssertEqual(events.activated, [next])
    }

    func testLateActivationDoesNotExtendTotalGracePastThreeSeconds() async throws {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let id = UUID()
        controller.begin(callID: id, action: makeAction(events))
        try controller.claimSucceeded(callID: id)
        controller.end(callID: id)
        events.now += 1.9
        controller.didActivate()
        events.now += 1.1
        XCTAssertFalse(controller.hasUnresolvedAudioEvents)
        XCTAssertTrue(events.activated.isEmpty)
    }

    func testWaitingAnswerHonorsItsEarlierActionDeadline() async throws {
        let events = AnswerControllerEvents()
        let clock = AudioDrainTestClock()
        let controller = makeController(events, clock: clock)
        defer { controller.reset() }
        let old = UUID()
        controller.begin(callID: old, action: makeAction(events))
        try controller.claimSucceeded(callID: old)
        controller.end(callID: old)
        let next = UUID()
        let action = FakeAnswerAction(deadline: clock.now.addingTimeInterval(0.5)) { _ in }
        controller.begin(callID: next, action: action)
        var ready: Bool?
        let waiter = Task { ready = await controller.waitUntilAudioDrained(answerID: next) }
        await settleTasks()
        clock.advance(0.5)
        await settleTasks()
        await waiter.value
        XCTAssertEqual(ready, false)
        XCTAssertEqual(events.failedIDs, [next])
        XCTAssertEqual(action.fulfillCount, 0)
        XCTAssertEqual(action.failCount, 0)
        XCTAssertTrue(controller.hasUnresolvedAudioEvents)
    }

    func testCancelledAdmissionDoesNotClaimAudioOrWaitUntilGraceEnds() async throws {
        let events = AnswerControllerEvents()
        let clock = AudioDrainTestClock()
        let controller = makeController(events, clock: clock)
        defer { controller.reset() }
        let old = UUID()
        controller.begin(callID: old, action: makeAction(events))
        try controller.claimSucceeded(callID: old)
        controller.end(callID: old)
        let waiter = Task { await controller.waitUntilAudioDrained() }
        await settleTasks()
        waiter.cancel()
        let ready = await waiter.value
        XCTAssertFalse(ready)
        XCTAssertTrue(controller.hasUnresolvedAudioEvents)
        XCTAssertTrue(events.activated.isEmpty)
    }

    func testGraceDurationIsInjectableAndExpiresWithoutPolling() async throws {
        let expired = expectation(description: "grace expired")
        let controller = CallKitAnswerController(
            activationGrace: 0.02, deactivationGrace: 0.03,
            configureAudio: { _ in }, activateAudio: { _ in },
            onFailure: { _, _ in }, onDeactivate: { _ in }
        )
        defer { controller.reset() }
        let id = UUID()
        controller.begin(callID: id, action: FakeAnswerAction(deadline: Date().addingTimeInterval(60)) { _ in })
        try controller.claimSucceeded(callID: id, stopAudio: { expired.fulfill() })
        controller.end(callID: id)
        await fulfillment(of: [expired], timeout: 1)
        XCTAssertFalse(controller.hasUnresolvedAudioEvents)
    }

    func testWallClockCorrectionsDoNotShortenOrExtendGrace() async throws {
        for correction in [-3600.0, 3600.0] {
            var wallNow = Date(timeIntervalSince1970: 1_000)
            let controller = CallKitAnswerController(
                now: { wallNow }, activationGrace: 0.02, deactivationGrace: 0.03, drainWaitTimeout: 0.04,
                configureAudio: { _ in }, activateAudio: { _ in },
                onFailure: { _, _ in }, onDeactivate: { _ in }
            )
            let id = UUID()
            controller.begin(callID: id, action: FakeAnswerAction(deadline: wallNow.addingTimeInterval(60)) { _ in })
            try controller.claimSucceeded(callID: id)
            controller.end(callID: id)
            wallNow += correction
            XCTAssertTrue(controller.hasUnresolvedAudioEvents, "wall-clock jump must not skip grace")
            let waiter = Task { await controller.waitUntilAudioDrained() }
            try await Task.sleep(for: .milliseconds(60))
            waiter.cancel()
            let ready = await waiter.value
            XCTAssertTrue(ready, "wall-clock rollback must not hold expired audio")
            controller.reset()
        }
    }

    private func settleTasks() async {
        for _ in 0..<30 { await Task.yield() }
    }

    func testAudioOwnershipIsNotReleasedUntilSystemDeactivationDrains() async throws {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let id = UUID()
        controller.begin(callID: id, action: makeAction(events))
        XCTAssertFalse(controller.hasUnresolvedAudioEvents)
        try controller.claimSucceeded(callID: id)
        XCTAssertTrue(controller.hasUnresolvedAudioEvents)
        controller.didActivate()
        _ = controller.end(callID: id)
        XCTAssertTrue(controller.hasUnresolvedAudioEvents)
        controller.didDeactivate()
        XCTAssertFalse(controller.hasUnresolvedAudioEvents)
    }

    func testClaimConfiguresAudioBeforeFulfillingAndDoesNotActivateIt() async throws {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let callID = UUID()
        let action = makeAction(events)

        controller.begin(callID: callID, action: action)
        XCTAssertTrue(events.operations.isEmpty)
        try controller.claimSucceeded(callID: callID)

        XCTAssertEqual(events.operations, ["configure", "fulfill"])
        XCTAssertEqual(action.fulfillCount, 1)
        XCTAssertEqual(action.failCount, 0)
        XCTAssertTrue(events.activated.isEmpty)
        controller.didActivate()
        XCTAssertEqual(events.activated, [callID])
    }

    func testDuplicateClaimDoesNotFulfillOrConfigureTwice() async throws {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let callID = UUID()
        let action = makeAction(events)

        controller.begin(callID: callID, action: action)
        try controller.claimSucceeded(callID: callID)
        try controller.claimSucceeded(callID: callID)

        XCTAssertEqual(events.operations, ["configure", "fulfill"])
        XCTAssertEqual(action.fulfillCount, 1)
    }

    func testClaimFailureFailsPendingActionOnlyOnceWithoutReportingEnded() async {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let callID = UUID()
        let action = makeAction(events)

        controller.begin(callID: callID, action: action)
        XCTAssertFalse(controller.end(callID: callID))
        _ = controller.end(callID: callID)

        XCTAssertEqual(action.failCount, 1)
        XCTAssertEqual(action.fulfillCount, 0)
        XCTAssertTrue(events.activated.isEmpty)
    }

    func testExpiredClaimDoesNotCompleteExpiredActionAndFailsAttemptOnce() async {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let callID = UUID()
        let action = makeAction(events)

        controller.begin(callID: callID, action: action)
        events.now = action.deadline.addingTimeInterval(1)
        XCTAssertThrowsError(try controller.claimSucceeded(callID: callID))
        controller.timedOut(callID: callID)

        XCTAssertEqual(action.failCount, 0)
        XCTAssertEqual(action.fulfillCount, 0)
        XCTAssertEqual(events.failedIDs, [callID])
        XCTAssertTrue(events.operations.isEmpty)
    }

    func testSystemActionTimeoutDoesNotFulfillOrFailExpiredAction() async {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let callID = UUID()
        let action = makeAction(events)

        controller.begin(callID: callID, action: action)
        controller.timedOut(callID: callID)
        controller.timedOut(callID: callID)
        XCTAssertTrue(controller.end(callID: callID))

        XCTAssertEqual(events.failedIDs, [callID])
        XCTAssertEqual(action.failCount, 0)
        XCTAssertEqual(action.fulfillCount, 0)
    }

    func testActivationTimeoutFailsFulfilledCallWithoutCompletingActionAgain() async throws {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let callID = UUID()
        let action = makeAction(events)

        controller.begin(callID: callID, action: action)
        try controller.claimSucceeded(callID: callID)
        controller.activationTimedOut(callID: callID)
        controller.activationTimedOut(callID: callID)
        XCTAssertTrue(controller.end(callID: callID))

        XCTAssertEqual(events.failedIDs, [callID])
        XCTAssertEqual(action.fulfillCount, 1)
        XCTAssertEqual(action.failCount, 0)
    }

    func testActivationBeforeDeadlineMakesLateActivationTimeoutHarmless() async throws {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let callID = UUID()

        controller.begin(callID: callID, action: makeAction(events))
        try controller.claimSucceeded(callID: callID)
        controller.didActivate()
        controller.activationTimedOut(callID: callID)

        XCTAssertEqual(events.activated, [callID])
        XCTAssertTrue(events.failedIDs.isEmpty)
    }

    func testPendingActionDeadlineAutomaticallyFailsAttempt() async {
        let failure = expectation(description: "answer deadline")
        let callID = UUID()
        var failedIDs: [UUID] = []
        let controller = CallKitAnswerController(
            configureAudio: { _ in },
            activateAudio: { _ in },
            onFailure: { failedID, _ in
                failedIDs.append(failedID)
                failure.fulfill()
            },
            onDeactivate: { _ in }
        )
        defer { controller.reset() }
        let action = FakeAnswerAction(deadline: Date().addingTimeInterval(0.02)) { _ in }

        controller.begin(callID: callID, action: action)
        await fulfillment(of: [failure], timeout: 1)

        XCTAssertEqual(failedIDs, [callID])
        XCTAssertEqual(action.failCount, 0)
        XCTAssertEqual(action.fulfillCount, 0)
    }

    func testFulfilledActionStartsIndependentActivationDeadline() async throws {
        let failure = expectation(description: "activation deadline")
        let callID = UUID()
        var failedIDs: [UUID] = []
        let controller = CallKitAnswerController(
            activationTimeout: .milliseconds(20),
            configureAudio: { _ in },
            activateAudio: { _ in },
            onFailure: { failedID, _ in
                failedIDs.append(failedID)
                failure.fulfill()
            },
            onDeactivate: { _ in }
        )
        defer { controller.reset() }
        let action = FakeAnswerAction(deadline: Date().addingTimeInterval(60)) { _ in }

        controller.begin(callID: callID, action: action)
        try controller.claimSucceeded(callID: callID)
        await fulfillment(of: [failure], timeout: 1)

        XCTAssertEqual(failedIDs, [callID])
        XCTAssertEqual(action.fulfillCount, 1)
        XCTAssertEqual(action.failCount, 0)
    }

    func testActivationErrorFailsAttemptInsteadOfSilentlyWaiting() async throws {
        let events = AnswerControllerEvents()
        events.activationError = true
        let controller = makeController(events)
        defer { controller.reset() }
        let callID = UUID()
        let action = makeAction(events)

        controller.begin(callID: callID, action: action)
        try controller.claimSucceeded(callID: callID)
        controller.didActivate()
        controller.activationTimedOut(callID: callID)

        XCTAssertEqual(events.failedIDs, [callID])
        XCTAssertEqual(action.fulfillCount, 1)
        XCTAssertEqual(action.failCount, 0)
    }

    func testLateActivationOfEndedCallDoesNotActivateNextCall() async throws {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let oldCall = UUID()
        let newCall = UUID()

        controller.begin(callID: oldCall, action: makeAction(events))
        try controller.claimSucceeded(callID: oldCall)
        XCTAssertTrue(controller.end(callID: oldCall))
        controller.begin(callID: newCall, action: makeAction(events))
        try controller.claimSucceeded(callID: newCall)

        controller.didActivate()
        XCTAssertTrue(events.activated.isEmpty)
        controller.didActivate()
        XCTAssertEqual(events.activated, [newCall])
    }

    func testLateDeactivationOfEndedCallDoesNotDeactivateNextCall() async throws {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        defer { controller.reset() }
        let oldCall = UUID()
        let newCall = UUID()

        controller.begin(callID: oldCall, action: makeAction(events))
        try controller.claimSucceeded(callID: oldCall)
        controller.didActivate()
        XCTAssertTrue(controller.end(callID: oldCall))
        controller.begin(callID: newCall, action: makeAction(events))
        try controller.claimSucceeded(callID: newCall)
        controller.didActivate()

        controller.didDeactivate()
        XCTAssertTrue(events.deactivated.isEmpty)
        controller.didDeactivate()
        XCTAssertEqual(events.deactivated, [newCall])
    }

    func testResetFailsPendingActionsAndDropsAudioEpochs() async throws {
        let events = AnswerControllerEvents()
        let controller = makeController(events)
        let activeCall = UUID()
        let pendingCall = UUID()
        let activeAction = makeAction(events)
        let pendingAction = makeAction(events)
        controller.begin(callID: activeCall, action: activeAction)
        try controller.claimSucceeded(callID: activeCall)
        controller.didActivate()
        controller.begin(callID: pendingCall, action: pendingAction)

        controller.reset()
        controller.didActivate()
        controller.didDeactivate()

        XCTAssertEqual(activeAction.fulfillCount, 1)
        XCTAssertEqual(activeAction.failCount, 0)
        XCTAssertEqual(pendingAction.failCount, 1)
        XCTAssertEqual(events.activated, [activeCall])
        XCTAssertTrue(events.deactivated.isEmpty)
    }

    private func makeAction(_ events: AnswerControllerEvents) -> FakeAnswerAction {
        FakeAnswerAction(deadline: events.now.addingTimeInterval(60)) {
            events.operations.append($0)
        }
    }

    private func makeController(_ events: AnswerControllerEvents, clock: AudioDrainTestClock? = nil) -> CallKitAnswerController {
        CallKitAnswerController(
            now: { clock?.now ?? events.now },
            monotonicNow: { clock?.elapsed ?? events.now.timeIntervalSince1970 },
            activationTimeout: .seconds(60),
            sleep: { duration in
                if let clock { try await clock.sleep(for: duration) }
                else { try await Task.sleep(for: duration) }
            },
            configureAudio: { _ in events.operations.append("configure") },
            activateAudio: { callID in
                if events.activationError { throw AnswerControllerTestError.activationFailed }
                events.activated.append(callID)
            },
            onFailure: { callID, _ in events.failedIDs.append(callID) },
            onDeactivate: { callID in events.deactivated.append(callID) }
        )
    }
}

@MainActor
private final class AudioDrainTestClock {
    var now = Date(timeIntervalSince1970: 1_000)
    var elapsed: TimeInterval = 0
    private var sleepers: [UUID: (TimeInterval, CheckedContinuation<Void, Error>)] = [:]

    func sleep(for duration: Duration) async throws {
        let id = UUID()
        let parts = duration.components
        let seconds = Double(parts.seconds) + Double(parts.attoseconds) / 1e18
        try await withTaskCancellationHandler {
            try Task.checkCancellation()
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                if Task.isCancelled { continuation.resume(throwing: CancellationError()) }
                else { sleepers[id] = (elapsed + seconds, continuation) }
            }
        } onCancel: {
            Task { @MainActor in
                self.sleepers.removeValue(forKey: id)?.1.resume(throwing: CancellationError())
            }
        }
    }

    func advance(_ seconds: TimeInterval) {
        now += seconds
        elapsed += seconds
        for (id, sleeper) in sleepers where sleeper.0 <= elapsed {
            sleepers.removeValue(forKey: id)?.1.resume()
        }
    }
}

@MainActor
private final class AnswerControllerEvents {
    var now = Date(timeIntervalSince1970: 1_000)
    var operations: [String] = []
    var activated: [UUID] = []
    var deactivated: [UUID] = []
    var failedIDs: [UUID] = []
    var activationError = false
}

@MainActor
private final class FakeAnswerAction: CallKitAnswerAction {
    let deadline: Date
    private(set) var isComplete = false
    private(set) var fulfillCount = 0
    private(set) var failCount = 0
    private let onOperation: (String) -> Void

    init(deadline: Date, onOperation: @escaping (String) -> Void) {
        self.deadline = deadline
        self.onOperation = onOperation
    }

    func fulfill() {
        fulfillCount += 1
        isComplete = true
        onOperation("fulfill")
    }

    func fail() {
        failCount += 1
        isComplete = true
        onOperation("fail")
    }
}

private enum AnswerControllerTestError: Error {
    case activationFailed
}
