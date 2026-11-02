import Foundation
import XCTest

final class CallKitConfigurationTests: XCTestCase {
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
        XCTAssertTrue(project.contains("CURRENT_PROJECT_VERSION: \"9\""))
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
