import Foundation
import XCTest
@testable import CallPilot

@MainActor
final class CallFirstFrameRendererRemovalTests: XCTestCase {
    func testFirstFrameRecordsThenRemovesAsynchronouslyOnlyOnce() async {
        var recorded = 0
        var removed = 0
        let observation = CallFirstFrameRendererRemoval {
            MainActor.assumeIsolated {
                XCTAssertEqual(recorded, 1)
                removed += 1
            }
        }
        observation.recordFirstFrame { recorded += 1 }
        observation.recordFirstFrame { XCTFail("Recorded a second frame") }
        XCTAssertEqual(recorded, 1)
        XCTAssertEqual(removed, 0)
        await drainMainQueue()
        XCTAssertEqual(removed, 1)

        observation.remove()
        observation.remove()
        observation.recordFirstFrame { XCTFail("Recorded after removal") }
        await drainMainQueue()
        XCTAssertEqual(removed, 1)
    }

    func testDisconnectBeforeQueuedRemovalRemovesOnlyOnce() async {
        var removed = 0
        let observation = CallFirstFrameRendererRemoval {
            MainActor.assumeIsolated { removed += 1 }
        }
        observation.recordFirstFrame {}
        XCTAssertEqual(removed, 0)
        observation.remove()
        observation.remove()
        XCTAssertEqual(removed, 1)
        await drainMainQueue()
        XCTAssertEqual(removed, 1)
    }

    func testDisconnectBeforeFirstFrameRemovesOnlyOnce() async {
        var removed = 0
        let observation = CallFirstFrameRendererRemoval {
            MainActor.assumeIsolated { removed += 1 }
        }
        observation.remove()
        observation.recordFirstFrame { XCTFail("Recorded after disconnect") }
        observation.remove()
        await drainMainQueue()
        XCTAssertEqual(removed, 1)
    }

    private func drainMainQueue() async {
        await withCheckedContinuation { continuation in
            DispatchQueue.main.async { continuation.resume() }
        }
    }
}
