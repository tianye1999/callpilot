import XCTest
@testable import CallPilot

final class SettingsStateTests: XCTestCase {
    func testDeviceStatusMovesThroughLoadingLiveAndStale() {
        var machine = DeviceStatusStateMachine()

        let first = machine.beginRefresh()
        XCTAssertEqual(machine.syncStatus, .loading)
        XCTAssertTrue(machine.succeed(
            HostedDeviceStatus(connected: true, modemOnline: true),
            for: first
        ))
        XCTAssertEqual(machine.syncStatus, .live)
        XCTAssertEqual(machine.status, HostedDeviceStatus(connected: true, modemOnline: true))

        let second = machine.beginRefresh()
        XCTAssertEqual(machine.syncStatus, .live, "refresh must not blank a known-good status")
        XCTAssertTrue(machine.fail(for: second))
        XCTAssertEqual(machine.syncStatus, .stale)
        XCTAssertEqual(machine.status, HostedDeviceStatus(connected: true, modemOnline: true))
    }

    func testDeviceStatusFailureWithoutSnapshotIsOffline() {
        var machine = DeviceStatusStateMachine()

        let attempt = machine.beginRefresh()
        XCTAssertTrue(machine.fail(for: attempt))

        XCTAssertEqual(machine.syncStatus, .offline)
        XCTAssertNil(machine.status)
    }

    func testCancellationRestoresIdleWithoutSnapshotAndPreservesLiveSnapshot() {
        var machine = DeviceStatusStateMachine()

        let initial = machine.beginRefresh()
        XCTAssertTrue(machine.cancel(for: initial))
        XCTAssertEqual(machine.syncStatus, .idle)

        let successful = machine.beginRefresh()
        XCTAssertTrue(machine.succeed(
            HostedDeviceStatus(connected: true, modemOnline: true),
            for: successful
        ))
        let refresh = machine.beginRefresh()
        XCTAssertTrue(machine.cancel(for: refresh))
        XCTAssertEqual(machine.syncStatus, .live)
        XCTAssertEqual(machine.status, HostedDeviceStatus(connected: true, modemOnline: true))
    }

    func testResetFencesLateDeviceStatusResponse() {
        var machine = DeviceStatusStateMachine()
        let oldAttempt = machine.beginRefresh()

        machine.reset()

        XCTAssertFalse(machine.succeed(
            HostedDeviceStatus(connected: true, modemOnline: true),
            for: oldAttempt
        ))
        XCTAssertEqual(machine.syncStatus, .idle)
        XCTAssertNil(machine.status)
    }

    func testNewRefreshFencesAnOlderInFlightResponse() {
        var machine = DeviceStatusStateMachine()
        let older = machine.beginRefresh()
        let current = machine.beginRefresh()

        XCTAssertTrue(machine.succeed(
            HostedDeviceStatus(connected: true, modemOnline: true),
            for: current
        ))
        XCTAssertFalse(machine.fail(for: older))
        XCTAssertEqual(machine.syncStatus, .live)
        XCTAssertEqual(machine.status, HostedDeviceStatus(connected: true, modemOnline: true))
    }
}
