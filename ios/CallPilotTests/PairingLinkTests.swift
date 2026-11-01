import XCTest
@testable import CallPilot

final class PairingLinkTests: XCTestCase {
    func testDefaultGatewayUsesSIMRelay() {
        XCTAssertEqual(PairingLink.defaultGateway, "https://sim-relay.bondings.ai")
    }

    func testDesktopLinkExtractsOriginAndNormalizesCode() {
        let parsed = PairingLink.parse("  https://sim-relay.bondings.ai/remote_dialer.html#pair=abcd-efgh\n")
        XCTAssertEqual(parsed.gatewayBase, "https://sim-relay.bondings.ai")
        XCTAssertEqual(parsed.code, "ABCDEFGH")
        XCTAssertEqual(PairingLink.formatCode(parsed.code!), "ABCD-EFGH")
    }

    func testBareCodeKeepsManualGatewayIndependent() {
        for input in ["abcd-efgh", "abcdefgh", " ABCD-EFGH\n"] {
            let parsed = PairingLink.parse(input)
            XCTAssertNil(parsed.gatewayBase)
            XCTAssertEqual(parsed.code, "ABCDEFGH")
        }
    }

    func testEncodedFragmentAndNonDefaultPort() {
        let parsed = PairingLink.parse("https://EXAMPLE.COM:8443/remote_dialer.html?ignored=yes#pair=abcd%2Defgh")
        XCTAssertEqual(parsed.gatewayBase, "https://example.com:8443")
        XCTAssertEqual(parsed.code, "ABCDEFGH")
        XCTAssertEqual(PairingLink.parse("https://example.com:443/#pair=ABCDEFGH").gatewayBase,
                       "https://example.com")
    }

    func testGatewayWithoutValidFragmentDoesNotInventACode() {
        for suffix in ["", "#pair=", "#pair=ABC", "?pair=ABCDEFGH", "#other=ABCDEFGH"] {
            let parsed = PairingLink.parse("https://example.com/remote_dialer.html" + suffix)
            XCTAssertEqual(parsed.gatewayBase, "https://example.com")
            XCTAssertNil(parsed.code)
        }
    }

    func testInvalidOrInsecureInputsAreRejected() {
        for input in ["", "garbage", "123", "ABCDEFGHI", "http://example.com/#pair=ABCDEFGH",
                      "callpilot://pair/ABCDEFGH", "https:///#pair=ABCDEFGH"] {
            let parsed = PairingLink.parse(input)
            XCTAssertNil(parsed.gatewayBase, input)
            XCTAssertNil(parsed.code, input)
        }
    }
}
