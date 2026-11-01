import Foundation
import XCTest
@testable import CallPilot

@MainActor
final class CallAudioOwnerTests: XCTestCase {
    func testGraceExpiryStopsOnlyItsOwnerAndNeverAReplacementCall() async throws {
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let old = UUID(), next = UUID()
        try await audio.acquire(old, mode: .callKit)
        audio.stopIfOwned(old)
        XCTAssertEqual(device.stopCount, 1)
        await audio.release(old, after: {}).value
        try await audio.acquire(next, mode: .standalone)
        let stops = device.stopCount
        audio.stopIfOwned(old)
        XCTAssertEqual(device.stopCount, stops)
        XCTAssertEqual(audio.owner, next)
        await audio.release(next, after: {}).value
    }

    func testCleanupStopsDeviceEvenWithoutTransportAndKeepsCallKitMode() async throws {
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let id = UUID()
        try await audio.acquire(id, mode: .callKit)
        await audio.release(id, after: {}).value
        XCTAssertEqual(device.modes, [.callKit])
        XCTAssertEqual(device.stopCount, 2)
        XCTAssertNil(audio.owner)
    }

    func testOldCleanupCannotStopNewOwner() async throws {
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let old = UUID(), next = UUID()
        try await audio.acquire(old, mode: .callKit)
        await audio.release(old, after: {}).value
        try await audio.acquire(next, mode: .standalone)
        let stops = device.stopCount
        await audio.release(old, after: {}).value
        XCTAssertEqual(device.stopCount, stops)
        XCTAssertThrowsError(try audio.activate(old))
        XCTAssertEqual(device.activateCount, 0)
        XCTAssertEqual(audio.owner, next)
        await audio.release(next, after: {}).value
    }

    func testNextOwnerWaitsUntilOldWorkHasSettled() async throws {
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let old = UUID(), next = UUID()
        try await audio.acquire(old, mode: .callKit)
        var continuation: CheckedContinuation<Void, Never>?
        let cleanup = audio.release(old) {
            await withCheckedContinuation { continuation = $0 }
        }
        while continuation == nil { await Task.yield() }
        let acquire = Task { try await audio.acquire(next, mode: .standalone) }
        await Task.yield()
        XCTAssertEqual(audio.owner, old)
        XCTAssertEqual(device.modes, [.callKit])
        continuation?.resume()
        await cleanup.value
        try await acquire.value
        XCTAssertEqual(audio.owner, next)
        XCTAssertEqual(device.modes, [.callKit, .standalone])
        await audio.release(next, after: {}).value
    }

    func testCleanupErrorIsReportedAndFinalStopIsRetried() async throws {
        let device = TestCallAudioDevice()
        var errors = 0
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in errors += 1 })
        let id = UUID()
        try await audio.acquire(id, mode: .standalone)
        device.stopError = TestAudioError.failed
        await audio.release(id, after: {}).value
        XCTAssertEqual(errors, 2)
        XCTAssertEqual(device.stopCount, 2)
    }
}

enum TestAudioError: Error { case failed }

@MainActor
final class TestCallAudioDevice: CallAudioDevice {
    var modes: [CallAudioMode] = []
    var configureCount = 0
    var activateCount = 0
    var stopCount = 0
    var stopError: Error?
    var activateError: Error?
    func prepare(mode: CallAudioMode) throws { modes.append(mode) }
    func configureCallKitSession() throws { configureCount += 1 }
    func activate() throws {
        activateCount += 1
        if let activateError { throw activateError }
    }
    func stop() throws {
        stopCount += 1
        if let stopError { throw stopError }
    }
}
