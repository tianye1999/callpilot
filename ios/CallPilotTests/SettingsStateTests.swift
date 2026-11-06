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
        XCTAssertEqual(machine.syncStatus, .live, "one failed poll must not disable dialing")
        XCTAssertEqual(machine.status?.lineReady, true)
        XCTAssertEqual(machine.status, HostedDeviceStatus(connected: true, modemOnline: true))

        let third = machine.beginRefresh()
        XCTAssertTrue(machine.fail(for: third))
        XCTAssertEqual(machine.syncStatus, .stale)
        XCTAssertEqual(machine.status, HostedDeviceStatus(connected: true, modemOnline: true))

        let fourth = machine.beginRefresh()
        XCTAssertTrue(machine.fail(for: fourth))
        XCTAssertEqual(machine.syncStatus, .stale)
    }

    func testRecoveryClearsConsecutiveFailures() {
        var machine = DeviceStatusStateMachine()
        let first = machine.beginRefresh()
        machine.succeed(HostedDeviceStatus(connected: true, modemOnline: true), for: first)
        for _ in 0..<2 {
            let refresh = machine.beginRefresh()
            machine.fail(for: refresh)
        }
        XCTAssertEqual(machine.syncStatus, .stale)

        let recovery = machine.beginRefresh()
        XCTAssertTrue(machine.succeed(HostedDeviceStatus(connected: true, modemOnline: true), for: recovery))
        XCTAssertEqual(machine.syncStatus, .live)
        let next = machine.beginRefresh()
        machine.fail(for: next)
        XCTAssertEqual(machine.syncStatus, .live)
        XCTAssertEqual(machine.status?.lineReady, true)
    }

    func testSingleFailurePreservesSuccessfulOfflineSnapshot() {
        var machine = DeviceStatusStateMachine()
        let first = machine.beginRefresh()
        let snapshot = HostedDeviceStatus(connected: false, modemOnline: false)
        machine.succeed(snapshot, for: first)

        let next = machine.beginRefresh()
        machine.fail(for: next)
        XCTAssertEqual(machine.syncStatus, .live)
        XCTAssertEqual(machine.status, snapshot)
        XCTAssertEqual(machine.status?.lineReady, false)
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

        let next = machine.beginRefresh()
        machine.fail(for: next)
        XCTAssertEqual(machine.syncStatus, .live, "ignored failures must not consume tolerance")
    }

    func testResetClearsFailureBudgetAndSnapshot() {
        var machine = DeviceStatusStateMachine()
        let first = machine.beginRefresh()
        machine.succeed(HostedDeviceStatus(connected: true, modemOnline: true), for: first)
        let failure = machine.beginRefresh()
        machine.fail(for: failure)

        machine.reset()
        let withoutSnapshot = machine.beginRefresh()
        machine.fail(for: withoutSnapshot)
        XCTAssertEqual(machine.syncStatus, .offline)
        XCTAssertNil(machine.status)

        let recovery = machine.beginRefresh()
        machine.succeed(HostedDeviceStatus(connected: true, modemOnline: true), for: recovery)
        let next = machine.beginRefresh()
        machine.fail(for: next)
        XCTAssertEqual(machine.syncStatus, .live)
    }
}
