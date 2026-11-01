import Foundation
import XCTest
@testable import CallPilot

final class AlertPushTests: XCTestCase {
    func testProfileOverridesBuildConfiguration() throws {
        for (entitlement, expected) in [("development", ApnsEnvironment.sandbox),
                                        ("production", ApnsEnvironment.production)] {
            let plist = try PropertyListSerialization.data(
                fromPropertyList: ["Entitlements": ["aps-environment": entitlement]],
                format: .xml, options: 0
            )
            let cms = Data([0x30, 0x82, 0xff]) + plist + Data([0xff, 0x00])
            XCTAssertEqual(PushEnvironment.resolve(profile: cms, debug: false), expected)
            XCTAssertEqual(PushEnvironment.resolve(profile: cms, debug: true), expected)
        }
    }

    func testMissingAndMalformedProfileFallbacks() {
        for profile in [nil, Data(), Data("<plist>broken</plist>".utf8)] {
            XCTAssertEqual(PushEnvironment.resolve(profile: profile, debug: false), .production)
            XCTAssertEqual(PushEnvironment.resolve(profile: profile, debug: true), .sandbox)
        }
        #if DEBUG
        XCTAssertEqual(PushEnvironment.resolve(profile: nil), .sandbox)
        #else
        XCTAssertEqual(PushEnvironment.resolve(profile: nil), .production)
        #endif
    }

    func testRoutesOnlyRecognizedAlerts() {
        for kind in ["sms", "call_digest", "missed_call"] {
            let route = AlertPushRoute.decode(["v": 1, "type": "device.notify", "kind": kind])
            XCTAssertEqual(route?.destination, kind == "sms" ? .messages : .records)
            XCTAssertNotEqual(route?.id, AlertPushRoute.decode(
                ["v": 1, "type": "device.notify", "kind": kind]
            )?.id)
        }
        XCTAssertNil(AlertPushRoute.decode(["v": true, "type": "device.notify", "kind": "sms"]))
        XCTAssertNil(AlertPushRoute.decode(["v": 1, "type": "inbound.offer", "kind": "sms"]))
        XCTAssertNil(AlertPushRoute.decode(["v": 1, "type": "device.notify", "kind": "unknown"]))
    }

    func testFeatureDefaultsOffAndLocalizationsExist() {
        XCTAssertFalse(AlertPushConfiguration.enabled(value: nil))
        XCTAssertFalse(AlertPushConfiguration.enabled(value: "NO"))
        XCTAssertTrue(AlertPushConfiguration.enabled(value: "YES"))
        for key in ["push.sms", "push.call_digest", "push.missed_call"] {
            XCTAssertNotEqual(L10n.text(key), key)
        }
    }
}
