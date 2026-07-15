import XCTest
@testable import CallPilot

final class CallStatusReducerTests: XCTestCase {
    func testMediaReadyProducesDialGateActionWithoutChangingState() {
        // Android parity: CallManagerTest.`完整生命周期 拨号到本地挂断` media_ready gate.
        let action = CallStatusReducer.reduce(
            .status(name: "media_ready", reason: nil, code: nil),
            label: "10086"
        )

        XCTAssertEqual(action, .mediaReady)
    }

    func testDialingAndConnectedMapToActiveStates() {
        // Android parity: CallManagerTest.`完整生命周期 拨号到本地挂断`.
        XCTAssertEqual(
            CallStatusReducer.reduce(.status(name: "dialing", reason: nil, code: nil), label: "10086"),
            .state(.dialing(number: "10086"))
        )
        XCTAssertEqual(
            CallStatusReducer.reduce(.remoteCall(status: "connected"), label: "10086"),
            .state(.inCall(label: "10086"))
        )
    }

    func testEndedAndHangupMapToTerminalState() {
        // Android parity: CallManagerTest.`Edge 结束事件驱动收尾`.
        XCTAssertEqual(
            CallStatusReducer.reduce(
                .status(name: "ended", reason: "user_hangup", code: nil),
                label: "10086"
            ),
            .state(.ended(label: "10086", reason: "user_hangup"))
        )
        XCTAssertEqual(
            CallStatusReducer.reduce(.remoteCall(status: "hangup"), label: "10086"),
            .state(.ended(label: "10086", reason: "hangup"))
        )
    }

    func testFailedMapsStableCodeAndReason() {
        // Android parity: CallManagerTest.`Edge failed 事件进入 Failed`.
        XCTAssertEqual(
            CallStatusReducer.reduce(
                .status(name: "failed", reason: nil, code: "MODEM_OFFLINE"),
                label: "10086"
            ),
            .state(.failed(label: "10086", reason: "MODEM_OFFLINE", code: "MODEM_OFFLINE"))
        )
    }

    func testLifecycleHintsAndFutureStatusesAreIgnored() {
        // Android parity: CallManager.handleEvent ignores waiting_for_phone and unknown statuses.
        XCTAssertEqual(
            CallStatusReducer.reduce(
                .status(name: "waiting_for_phone", reason: nil, code: nil),
                label: "10086"
            ),
            .ignored
        )
        XCTAssertEqual(
            CallStatusReducer.reduce(.remoteCall(status: "future"), label: "10086"),
            .ignored
        )
    }
}
