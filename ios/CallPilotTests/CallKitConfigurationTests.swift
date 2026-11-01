import Foundation
import XCTest

final class CallKitConfigurationTests: XCTestCase {
    func testVersionAndInternalProductIdentityRemainExplicit() throws {
        let project = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/project.yml"), encoding: .utf8
        )
        XCTAssertTrue(project.contains("MARKETING_VERSION: \"0.7.5\""))
        XCTAssertTrue(project.contains("CURRENT_PROJECT_VERSION: \"6\""))
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
        XCTAssertTrue(tokenCallback.contains("#if DEBUG\n            let environment = ApnsEnvironment.sandbox\n            #else\n            let environment = ApnsEnvironment.production\n            #endif"))
        XCTAssertTrue(tokenCallback.contains("self.delegate?.callKitDidUpdateToken(token, environment: environment)"))
    }

    func testVoipBackgroundModeAndPushEntitlementAreDeclared() throws {
        let info = try plist("ios/CallPilot/Info.plist")
        XCTAssertEqual(info["UIBackgroundModes"] as? [String], ["voip"])

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
