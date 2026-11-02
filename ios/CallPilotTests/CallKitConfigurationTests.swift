import Foundation
import XCTest

final class CallKitConfigurationTests: XCTestCase {
    func testEndedResultTimerUsesFiveSecondsAndFencesDismissal() throws {
        let source = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/CallPilot/AppModel.swift"), encoding: .utf8
        )
        XCTAssertTrue(source.contains("let endedResultDisplayDuration: Duration = .seconds(5)"))
        let timer = try section(source, from: "private func scheduleCallResultDismissal", to: "func dismissCallResult()")
        try assertBefore("callResultTask?.cancel()", "guard case .ended = state else { return }", in: timer)
        XCTAssertTrue(timer.contains("Task { [weak self]"))
        XCTAssertTrue(timer.contains("let duration = endedResultDisplayDuration"))
        try assertBefore("try await Task.sleep(for: duration)", "callAttempts.isCurrentEndedResult(state, for: attempt)", in: timer)
        try assertBefore("callAttempts.isCurrentEndedResult(state, for: attempt)", "dismissCallResult()", in: timer)
        let apply = try section(source, from: "private func apply(", to: "private func registerCurrentVoipToken")
        try assertBefore("callState = nextState", "scheduleCallResultDismissal(for: attempt, state: nextState)", in: apply)
        let reset = try section(source, from: "func dismissCallResult()", to: "func hangup()")
        XCTAssertTrue(reset.contains("callResultTask?.cancel()"))
        let begin = try section(source, from: "private func beginCallAttempt", to: "private func canBeginAudio")
        XCTAssertTrue(begin.contains("callResultTask?.cancel()"))
    }

    func testIncomingOfferResetsTerminalResultBeforeAdmissionAndPreconnectDecision() throws {
        let source = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/CallPilot/AppModel.swift"), encoding: .utf8
        )
        let offer = try section(source, from: "func callKitDidReceiveOffer", to: "func callKitDidRequestAnswer")
        try assertBefore("dismissCallResult()", "guard callKitCanAcceptIncomingCall", in: offer)
        try assertBefore("guard callKitCanAcceptIncomingCall", "incomingOffer = offer", in: offer)
        try assertBefore("incomingOffer = offer", "if claimPrewarmer.canPreconnect", in: offer)
        XCTAssertTrue(offer.contains("session.startSilentPreconnect(client: client, offer: offer)"))
        XCTAssertTrue(offer.contains("claimPrewarmer.receive(offer)"))
    }

    func testAnswerResetsTerminalResultBeforeFailureGuardAndTakeover() throws {
        let source = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/CallPilot/AppModel.swift"), encoding: .utf8
        )
        let answer = try section(source, from: "func callKitDidRequestAnswer", to: "func callKitDidRequestDecline")
        try assertBefore("dismissCallResult()", "guard callKitCanAcceptIncomingCall", in: answer)
        try assertBefore("guard callKitCanAcceptIncomingCall", "callKit.finishActiveCall(reason: .failed)", in: answer)
        XCTAssertTrue(answer.contains("performTakeover(offer, usesCallKit: true, timing: timing)"))

        let acceptance = try section(source, from: "var callKitCanAcceptIncomingCall:", to: "func callKitDidUpdateToken")
        XCTAssertTrue(acceptance.contains("pairing != nil && client != nil && callState == .idle"))
        let reset = try section(source, from: "func dismissCallResult()", to: "func hangup()")
        try assertBefore("guard callAttempts.resetTerminal() else { return }", "callState = .idle", in: reset)
        for assignment in ["media = nil", "callKitOfferId = nil", "speakerphoneEnabled = false"] {
            XCTAssertTrue(reset.contains(assignment))
        }
        XCTAssertFalse(reset.contains("media?.cancel"))
        XCTAssertFalse(reset.contains("mediaTimeoutTask?.cancel"))
        XCTAssertFalse(reset.contains(".stop("))
        XCTAssertFalse(reset.contains("finishActiveCall"))
    }

    func testAppHangupRegistersIntentWithoutAnAsynchronousActorHop() throws {
        let source = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/CallPilot/AppModel.swift"), encoding: .utf8
        )
        let button = try section(source, from: "func hangup()", to: "func sendDTMF")
        XCTAssertTrue(button.contains("callKit.requestEndActiveIfManaged()"))
        XCTAssertTrue(button.contains("media?.requestHangup()"))
        XCTAssertFalse(button.contains("Task {"))
        XCTAssertFalse(button.contains("await "))
        let delegate = try section(source, from: "func callKitDidRequestHangup", to: "func callKitPrepareToAnswer")
        XCTAssertTrue(delegate.contains("guard callKitOfferId == offerId"))
        XCTAssertTrue(delegate.contains("media?.requestHangup()"))
        XCTAssertFalse(delegate.contains("Task {"))
    }

    func testCallKitEndAndResetRegisterIntentBeforeAudioDrainAndFulfill() throws {
        let source = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/CallPilot/Call/CallKitCoordinator.swift"),
            encoding: .utf8
        )
        let ending = try section(source, from: "perform action: CXEndCallAction", to: "didActivate audioSession")
        try assertBefore("callKitDidRequestHangup", "answers.end(", in: ending)
        try assertBefore("callKitDidRequestHangup", "action.fulfill()", in: ending)
        let reset = try section(source, from: "func providerDidReset", to: "perform action: CXAnswerCallAction")
        try assertBefore("callKitDidRequestHangup", "answers.reset()", in: reset)
    }

    func testEndRequestsCaptureThisCallAndRegisterBeforeTransactionOrFailureCleanup() throws {
        let source = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/CallPilot/Call/CallKitCoordinator.swift"),
            encoding: .utf8
        )
        let active = try section(source, from: "func requestEndActiveIfManaged", to: "func markConnected")
        XCTAssertTrue(active.hasPrefix("func requestEndActiveIfManaged() -> Bool"))
        try assertBefore("let callUUID = pendingOrActiveCallUUID()", "Task {", in: active)
        try assertBefore("callKitDidRequestHangup", "Task {", in: active)
        let activeTask = try XCTUnwrap(active.components(separatedBy: "Task {").last)
        XCTAssertFalse(activeTask.contains("pendingOrActiveCallUUID()"))
        XCTAssertTrue(activeTask.contains("CXEndCallAction(call: callUUID)"))

        let managed = try section(source, from: "func requestEndIfManaged", to: "func requestEndActiveIfManaged")
        try assertBefore("callKitDidRequestHangup", "try await request(", in: managed)
        for method in [active, managed] {
            let failure = try XCTUnwrap(method.components(separatedBy: "} catch {").last)
            try assertBefore("callKitDidRequestHangup", "finish(callUUID:", in: failure)
        }
    }

    func testIncomingAndOutboundAdmissionAwaitDrainInsteadOfRejectingIncomingPush() throws {
        let source = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/CallPilot/AppModel.swift"), encoding: .utf8
        )
        let outbound = try XCTUnwrap(source.components(separatedBy: "func startCall(number:").last?
            .components(separatedBy: "func answerTakeover").first)
        let incoming = try XCTUnwrap(source.components(separatedBy: "private func performTakeover").last?
            .components(separatedBy: "private func scheduleMediaTimeout").first)
        let acceptance = try XCTUnwrap(source.components(separatedBy: "var callKitCanAcceptIncomingCall:").last?
            .components(separatedBy: "func callKitDidUpdateToken").first)
        XCTAssertTrue(outbound.contains("await canBeginAudio"))
        XCTAssertTrue(incoming.contains("await canBeginAudio"))
        XCTAssertFalse(acceptance.contains("isAudioSessionDrained"))
        XCTAssertTrue(source.contains("await callKit.waitUntilAudioDrained(offerId: callKitOfferId)"))
    }

    func testVersionAndInternalProductIdentityRemainExplicit() throws {
        let project = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/project.yml"), encoding: .utf8
        )
        XCTAssertTrue(project.contains("MARKETING_VERSION: \"0.7.5\""))
        XCTAssertTrue(project.contains("CURRENT_PROJECT_VERSION: \"15\""))
        XCTAssertTrue(project.contains("PRODUCT_BUNDLE_IDENTIFIER: ai.bondings.callpilot.sim\n"))
        XCTAssertTrue(project.contains("name: CallPilot\n"))
        XCTAssertTrue(project.contains("DEVELOPMENT_TEAM: ${DEVELOPMENT_TEAM}"))
    }

    func testCallKitUsesSIMDisplayNameAndBuildSpecificPushEnvironment() throws {
        let source = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/CallPilot/Call/CallKitCoordinator.swift"),
            encoding: .utf8
        )
        XCTAssertTrue(source.contains("CXHandle(type: .generic, value: \"KinyAI SIM\")"))
        XCTAssertTrue(source.contains("CXProviderConfiguration()"))
        let tokenCallback = try XCTUnwrap(source.components(separatedBy: "didUpdate pushCredentials").last?
            .components(separatedBy: "didInvalidatePushTokenFor").first)
        XCTAssertTrue(tokenCallback.contains("let environment = PushEnvironment.current()"))
        XCTAssertFalse(tokenCallback.contains("#if DEBUG"))
        XCTAssertTrue(tokenCallback.contains("self.delegate?.callKitDidUpdateToken(token, environment: environment)"))
    }

    func testVoipBackgroundModeAndPushEntitlementAreDeclared() throws {
        let info = try plist("ios/CallPilot/Info.plist")
        XCTAssertEqual(info["UIBackgroundModes"] as? [String], ["voip", "audio"])

        let entitlements = try plist("ios/CallPilot/CallPilot.entitlements")
        XCTAssertEqual(entitlements["aps-environment"] as? String, "development")

        let project = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/project.yml"),
            encoding: .utf8
        )
        XCTAssertTrue(project.contains("CODE_SIGN_ENTITLEMENTS: CallPilot/CallPilot.entitlements"))
        XCTAssertFalse(project.contains("APS_ENVIRONMENT:"))
    }

    func testVoipPushReportsToCallKitWithoutAnAsynchronousActorHop() throws {
        let source = try String(
            contentsOf: repositoryRoot.appendingPathComponent(
                "ios/CallPilot/Call/CallKitCoordinator.swift"
            ),
            encoding: .utf8
        )
        let callback = try XCTUnwrap(
            source.components(separatedBy: "didReceiveIncomingPushWith payload").last?
                .components(separatedBy: "extension CallKitCoordinator: CXProviderDelegate").first
        )

        XCTAssertTrue(callback.contains("MainActor.assumeIsolated"))
        XCTAssertFalse(callback.contains("Task { @MainActor"))
        XCTAssertTrue(callback.contains("self.report(decoded, completion: completionBox)"))
    }

    private func section(_ source: String, from start: String, to end: String) throws -> String {
        let startRange = try XCTUnwrap(source.range(of: start))
        let endRange = try XCTUnwrap(source.range(of: end, range: startRange.upperBound..<source.endIndex))
        return String(source[startRange.lowerBound..<endRange.lowerBound])
    }

    private func assertBefore(_ first: String, _ second: String, in source: String,
                              file: StaticString = #filePath, line: UInt = #line) throws {
        let firstRange = try XCTUnwrap(source.range(of: first), file: file, line: line)
        let secondRange = try XCTUnwrap(source.range(of: second), file: file, line: line)
        XCTAssertLessThan(firstRange.lowerBound, secondRange.lowerBound, file: file, line: line)
    }

    private func plist(_ path: String) throws -> [String: Any] {
        try XCTUnwrap(
            PropertyListSerialization.propertyList(
                from: Data(contentsOf: repositoryRoot.appendingPathComponent(path)),
                options: [],
                format: nil
            ) as? [String: Any]
        )
    }

    private var repositoryRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }
}
