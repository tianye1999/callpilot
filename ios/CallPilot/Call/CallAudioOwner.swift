import Foundation

enum CallAudioMode: Equatable {
    case standalone
    case callKit
}

@MainActor
protocol CallAudioDevice: AnyObject {
    func prepare(mode: CallAudioMode) throws
    func configureCallKitSession() throws
    func activate() throws
    func stop() throws
}

/// One process-wide audio lease. A new call cannot acquire it until old SDK
/// tasks have settled, including an unpublished concurrent microphone track.
@MainActor
final class CallAudioOwner {
    private let device: any CallAudioDevice
    private let onCleanupError: (Error) -> Void
    private(set) var owner: UUID?
    private var cleanupTask: Task<Void, Never>?

    init(device: any CallAudioDevice, onCleanupError: @escaping (Error) -> Void) {
        self.device = device
        self.onCleanupError = onCleanupError
    }

    func acquire(_ id: UUID, mode: CallAudioMode) async throws {
        while let cleanupTask {
            await cleanupTask.value
            try Task.checkCancellation()
        }
        try Task.checkCancellation()
        guard owner == nil else { throw CallAudioError.busy }
        owner = id
        try device.prepare(mode: mode)
    }

    func configureCallKitSession(_ id: UUID) throws {
        try requireOwner(id)
        try device.configureCallKitSession()
    }

    func activate(_ id: UUID) throws {
        try requireOwner(id)
        try device.activate()
    }

    private func requireOwner(_ id: UUID) throws {
        guard owner == id, cleanupTask == nil else { throw CancellationError() }
    }

    func stopIfOwned(_ id: UUID) {
        guard owner == id else { return }
        stopDevice()
    }

    @discardableResult
    func release(_ id: UUID, after operation: @escaping @MainActor () async -> Void) -> Task<Void, Never> {
        guard owner == id else {
            // Still reap this attempt's tasks, but never touch another call's device.
            return Task { await operation() }
        }
        if let cleanupTask { return cleanupTask }
        stopDevice()
        let task = Task {
            await operation()
            guard self.owner == id else { return }
            self.stopDevice()
            self.owner = nil
            self.cleanupTask = nil
        }
        cleanupTask = task
        return task
    }

    private func stopDevice() {
        do { try device.stop() }
        catch { onCleanupError(error) }
    }
}

enum CallAudioError: Error {
    case busy
    case answerExpired
}
