import XCTest
@testable import CallPilot

final class CallStateTests: XCTestCase {
    func testEndedDismissalRequiresSameAttemptAndExactEndedState() {
        var machine = CallAttemptStateMachine()
        let ended = CallState.ended(label: "previous", reason: "local_hangup")
        let attempt = machine.begin(with: ended)
        XCTAssertTrue(machine.isCurrentEndedResult(ended, for: attempt))
        XCTAssertTrue(machine.transition(to: .ended(label: "previous", reason: "remote_hangup"), for: attempt))
        XCTAssertFalse(machine.isCurrentEndedResult(ended, for: attempt))
        let failed = CallState.failed(label: "previous", reason: "network", code: nil)
        XCTAssertTrue(machine.transition(to: failed, for: attempt))
        XCTAssertFalse(machine.isCurrentEndedResult(failed, for: attempt))
        XCTAssertFalse(machine.isCurrentEndedResult(ended, for: attempt))
    }

    func testEndedDismissalCannotAffectManualResetOrReplacementCall() {
        for resetBeforeNextCall in [false, true] {
            var machine = CallAttemptStateMachine()
            let ended = CallState.ended(label: "call", reason: "ended")
            let old = machine.begin(with: ended)
            if resetBeforeNextCall {
                XCTAssertTrue(machine.resetTerminal())
                XCTAssertFalse(machine.isCurrentEndedResult(ended, for: old))
            }
            let next = machine.begin(with: .preparing(label: "call"))
            XCTAssertFalse(machine.isCurrentEndedResult(ended, for: old))
            XCTAssertTrue(machine.transition(to: ended, for: next))
            XCTAssertFalse(machine.isCurrentEndedResult(ended, for: old))
            XCTAssertTrue(machine.isCurrentEndedResult(ended, for: next))
        }
    }

    func testIncomingResetOnlyClearsTerminalStatesAndFencesTheirCallbacks() {
        let states: [CallState] = [
            .idle, .preparing(label: "previous"), .waitingMedia(label: "previous"),
            .dialing(number: "test-number"), .inCall(label: "previous"),
            .ended(label: "previous", reason: "local_hangup"),
            .failed(label: "previous", reason: "timeout", code: "MEDIA_TIMEOUT"),
        ]
        for state in states {
            var machine = CallAttemptStateMachine()
            let previous = machine.begin(with: state)
            XCTAssertEqual(machine.resetTerminal(), state.isTerminal, "\(state)")
            XCTAssertEqual(machine.state, state.isTerminal ? .idle : state)
            XCTAssertEqual(machine.isCurrent(previous), !state.isTerminal)
            XCTAssertEqual(machine.state == .idle, !state.isActive)
            if state.isTerminal {
                XCTAssertFalse(machine.state.isCallPresented)
                XCTAssertFalse(machine.transition(to: state, for: previous))
                let incoming = machine.begin(with: .preparing(label: "incoming"))
                XCTAssertTrue(machine.isCurrent(incoming))
            }
        }
    }

    func testTerminalStatesAreNotActive() {
        // Android parity: CallManagerTest uses the same active-state gate before every startCall.
        XCTAssertFalse(CallState.idle.isActive)
        XCTAssertFalse(CallState.ended(label: "10086", reason: "ended").isActive)
        XCTAssertFalse(CallState.failed(label: "10086", reason: "busy", code: "LINE_BUSY").isActive)
        XCTAssertTrue(CallState.preparing(label: "10086").isActive)
        XCTAssertTrue(CallState.waitingMedia(label: "10086").isActive)
        XCTAssertTrue(CallState.inCall(label: "10086").isActive)
    }

    func testTerminalStatesRemainPresentedUntilExplicitReset() {
        XCTAssertFalse(CallState.idle.isCallPresented)
        XCTAssertTrue(CallState.preparing(label: "10086").isCallPresented)
        XCTAssertTrue(CallState.ended(label: "10086", reason: "ended").isCallPresented)
        XCTAssertTrue(CallState.failed(label: "10086", reason: "busy", code: "LINE_BUSY").isCallPresented)

        var failedMachine = CallAttemptStateMachine()
        let failedAttempt = failedMachine.begin(with: .waitingMedia(label: "10086"))
        XCTAssertTrue(failedMachine.transition(
            to: .failed(label: "10086", reason: "busy", code: "LINE_BUSY"),
            for: failedAttempt
        ))
        XCTAssertTrue(failedMachine.resetTerminal())
        XCTAssertEqual(failedMachine.state, .idle)

        var endedMachine = CallAttemptStateMachine()
        let endedAttempt = endedMachine.begin(with: .inCall(label: "10086"))
        XCTAssertTrue(endedMachine.transition(
            to: .ended(label: "10086", reason: "remote_hangup"),
            for: endedAttempt
        ))
        XCTAssertTrue(endedMachine.resetTerminal())
        XCTAssertEqual(endedMachine.state, .idle)
    }

    func testExplicitTerminalResetFencesLateAttemptAndCannotResetActiveCall() {
        var machine = CallAttemptStateMachine()
        let oldAttempt = machine.begin(with: .waitingMedia(label: "old"))
        XCTAssertTrue(machine.transition(
            to: .failed(label: "old", reason: "timeout", code: "TAKEOVER_MEDIA_TIMEOUT"),
            for: oldAttempt
        ))
        XCTAssertTrue(machine.resetTerminal())
        XCTAssertFalse(machine.transition(to: .inCall(label: "old"), for: oldAttempt))

        let newAttempt = machine.begin(with: .waitingMedia(label: "new"))
        XCTAssertFalse(machine.resetTerminal())
        XCTAssertEqual(machine.state, .waitingMedia(label: "new"))
        XCTAssertTrue(machine.isCurrent(newAttempt))
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

    func testMediaWatchdogIgnoresChineseAndEnglishDisplayLabelChanges() {
        for initialLabel in ["来电转接", "Incoming handoff"] {
            var machine = CallAttemptStateMachine()
            let attempt = machine.begin(with: .waitingMedia(label: initialLabel))
            XCTAssertTrue(machine.transition(to: .waitingMedia(label: "来电接管"), for: attempt))

            XCTAssertTrue(machine.isWaitingForMedia(attempt), initialLabel)
        }
    }

    func testMediaWatchdogRejectsOldAttemptEvenWhenNewAttemptIsWaiting() {
        var machine = CallAttemptStateMachine()
        let oldAttempt = machine.begin(with: .waitingMedia(label: "Incoming handoff"))
        let newAttempt = machine.begin(with: .waitingMedia(label: "来电转接"))

        XCTAssertFalse(machine.isWaitingForMedia(oldAttempt))
        XCTAssertTrue(machine.isWaitingForMedia(newAttempt))
    }

    func testMediaWatchdogRejectsConnectedAndTerminalPhases() {
        let nonWaitingStates: [CallState] = [
            .idle,
            .preparing(label: "Incoming handoff"),
            .dialing(number: "10086"),
            .inCall(label: "来电转接"),
            .ended(label: "Incoming handoff", reason: "remote_hangup"),
            .failed(label: "来电转接", reason: "timeout", code: "TAKEOVER_MEDIA_TIMEOUT"),
        ]
        for state in nonWaitingStates {
            var machine = CallAttemptStateMachine()
            let attempt = machine.begin(with: .waitingMedia(label: "Incoming handoff"))
            XCTAssertTrue(machine.transition(to: state, for: attempt))

            XCTAssertFalse(machine.isWaitingForMedia(attempt), "\(state)")
        }
    }

    func testLateTerminalResetFromOldGenerationCannotResetNewAttempt() {
        // Android parity: CallManagerTest fences an old Failed result before a new attempt.
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

    func testCallResultCountdownTicksDownAndCompletes() {
        var countdown = CallResultCountdown(seconds: 3)
        XCTAssertEqual(countdown.remaining, 3)

        XCTAssertFalse(countdown.tick())
        XCTAssertEqual(countdown.remaining, 2)
        XCTAssertFalse(countdown.tick())
        XCTAssertEqual(countdown.remaining, 1)
        XCTAssertTrue(countdown.tick())
        XCTAssertEqual(countdown.remaining, 0)
    }

    func testCallResultCountdownCancelStopsTicks() {
        var countdown = CallResultCountdown(seconds: 6)
        countdown.cancel()
        XCTAssertTrue(countdown.cancelled)
        XCTAssertFalse(countdown.tick())
        XCTAssertEqual(countdown.remaining, 0)
    }

    func testIsCurrentTerminalResultFencesOldAttemptAndManualReset() {
        var machine = CallAttemptStateMachine()
        let ended = CallState.ended(label: "10086", reason: "remote_hangup")
        let old = machine.begin(with: ended)
        XCTAssertTrue(machine.isCurrentTerminalResult(ended, for: old))

        let failed = CallState.failed(label: "10086", reason: "busy", code: "LINE_BUSY")
        XCTAssertTrue(machine.transition(to: failed, for: old))
        XCTAssertTrue(machine.isCurrentTerminalResult(failed, for: old))
        XCTAssertFalse(machine.isCurrentTerminalResult(ended, for: old))

        let next = machine.begin(with: .preparing(label: "10010"))
        XCTAssertFalse(machine.isCurrentTerminalResult(failed, for: old))
        XCTAssertFalse(machine.isCurrentTerminalResult(ended, for: old))
        XCTAssertTrue(machine.isCurrent(next))

        _ = machine.transition(to: .failed(label: "10010", reason: "network", code: nil), for: next)
        XCTAssertTrue(machine.resetTerminal())
        XCTAssertFalse(machine.isCurrentTerminalResult(failed, for: old))
    }
}
