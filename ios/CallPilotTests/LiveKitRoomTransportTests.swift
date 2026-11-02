import XCTest
@testable import CallPilot

@MainActor
final class LiveKitRoomTransportTests: XCTestCase {
    func testCallKitDisablesConcurrentMicAndStandaloneRetainsIt() {
        XCTAssertFalse(LiveKitRoomTransport.silentConnectOptions.autoSubscribe)
        XCTAssertFalse(LiveKitRoomTransport.silentConnectOptions.enableMicrophone)
        XCTAssertFalse(LiveKitRoomTransport.connectOptions(enableMicrophone: false).enableMicrophone)
        XCTAssertTrue(LiveKitRoomTransport.connectOptions(enableMicrophone: true).enableMicrophone)
        XCTAssertEqual(LiveKitRoomTransport.controlPublishOptions.topic, CallPilotTopic.control)
        XCTAssertTrue(LiveKitRoomTransport.controlPublishOptions.reliable)
    }
}
