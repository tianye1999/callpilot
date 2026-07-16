import XCTest
@testable import CallPilot

final class MainTabShellTests: XCTestCase {
    func testMainTabsHaveStableIdentityAndOrder() {
        XCTAssertEqual(
            MainTab.allCases,
            [.dial, .records, .messages, .settings]
        )
        XCTAssertEqual(
            MainTab.allCases.map(\.rawValue),
            ["dial", "records", "messages", "settings"]
        )
    }
}
