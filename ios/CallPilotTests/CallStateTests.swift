import XCTest
@testable import CallPilot

final class CallStateTests: XCTestCase {
    func testTerminalStatesAreNotActive() {
        // Android parity: CallManagerTest uses the same active-state gate before every startCall.
        XCTAssertFalse(CallState.idle.isActive)
        XCTAssertFalse(CallState.ended(label: "10086", reason: "ended").isActive)
        XCTAssertFalse(CallState.failed(label: "10086", reason: "busy", code: "LINE_BUSY").isActive)
        XCTAssertTrue(CallState.preparing(label: "10086").isActive)
        XCTAssertTrue(CallState.waitingMedia(label: "10086").isActive)
        XCTAssertTrue(CallState.inCall(label: "10086").isActive)
    }

    func testLateCompletionFromPreviousAttemptCannotMutateCurrentAttempt() {
        // Android parity: CallManagerTest.`hosted 会话轮询中挂断不会在轮询完成后复活通话`.
        var machine = CallAttemptStateMachine()
        let first = machine.begin(with: .waitingMedia(label: "first"))
        let second = machine.begin(with: .waitingMedia(label: "second"))

        XCTAssertFalse(machine.transition(to: .inCall(label: "first"), for: first))
        XCTAssertEqual(machine.state, .waitingMedia(label: "second"))
        XCTAssertTrue(machine.transition(to: .inCall(label: "second"), for: second))
    }

    func testTimeoutRequiresMatchingGenerationAndExpectedState() {
        // Android parity: CallManagerTest.`answerTakeover 等待媒体超时后清理并自动回到 Idle`.
        var machine = CallAttemptStateMachine()
        let attempt = machine.begin(with: .waitingMedia(label: "来电接管"))
        XCTAssertTrue(machine.transition(
            from: .waitingMedia(label: "来电接管"),
            to: .failed(
                label: "来电接管",
                reason: "接管媒体建立超时",
                code: "TAKEOVER_MEDIA_TIMEOUT"
            ),
            for: attempt
        ))

        let connectedAttempt = machine.begin(with: .waitingMedia(label: "来电接管"))
        XCTAssertTrue(machine.transition(to: .inCall(label: "来电接管"), for: connectedAttempt))
        XCTAssertFalse(machine.transition(
            from: .waitingMedia(label: "来电接管"),
            to: .failed(label: "来电接管", reason: "late timeout", code: nil),
            for: connectedAttempt
        ))
        XCTAssertEqual(machine.state, .inCall(label: "来电接管"))
    }

    func testTimeoutFromOldGenerationCannotFailNewWaitingAttempt() {
        // Android parity: CallManager.armTakeoverMediaTimeout rejects session !== expectedSession.
        var machine = CallAttemptStateMachine()
        let oldAttempt = machine.begin(with: .waitingMedia(label: "来电接管"))
        _ = machine.begin(with: .waitingMedia(label: "来电接管"))

        XCTAssertFalse(machine.transition(
            from: .waitingMedia(label: "来电接管"),
            to: .failed(
                label: "来电接管",
                reason: "接管媒体建立超时",
                code: "TAKEOVER_MEDIA_TIMEOUT"
            ),
            for: oldAttempt
        ))
        XCTAssertEqual(machine.state, .waitingMedia(label: "来电接管"))
    }

    func testOldFailureVisibilityTimerCannotResetNewAttempt() {
        // Android parity: CallManagerTest compares the exact Failed value before returning to Idle.
        var machine = CallAttemptStateMachine()
        let first = machine.begin(with: .failed(label: "first", reason: "timeout", code: nil))
        let second = machine.begin(with: .failed(label: "second", reason: "network", code: nil))

        XCTAssertFalse(machine.transition(
            from: .failed(label: "first", reason: "timeout", code: nil),
            to: .idle,
            for: first
        ))
        XCTAssertEqual(machine.state, .failed(label: "second", reason: "network", code: nil))
        XCTAssertTrue(machine.isCurrent(second))
    }

    func testInvalidateRejectsAllOutstandingCompletions() {
        // Android parity: CallManagerTest.`挂断与 media_ready 交错时绝不发送 dial`.
        var machine = CallAttemptStateMachine()
        let attempt = machine.begin(with: .waitingMedia(label: "10086"))
        machine.invalidate(to: .ended(label: "10086", reason: "local_hangup"))

        XCTAssertFalse(machine.transition(to: .dialing(number: "10086"), for: attempt))
        XCTAssertEqual(machine.state, .ended(label: "10086", reason: "local_hangup"))
    }
}
