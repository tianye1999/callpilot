import Foundation

enum DeviceStatusSyncState: Equatable {
    case idle
    case loading
    case live
    case stale
    case offline
}

struct DeviceStatusRefresh: Equatable {
    fileprivate let generation: Int
}

struct DeviceStatusStateMachine {
    private(set) var status: HostedDeviceStatus?
    private(set) var syncStatus: DeviceStatusSyncState = .idle
    private var generation = 0

    mutating func beginRefresh() -> DeviceStatusRefresh {
        generation += 1
        if status == nil { syncStatus = .loading }
        return DeviceStatusRefresh(generation: generation)
    }

    @discardableResult
    mutating func succeed(
        _ status: HostedDeviceStatus,
        for refresh: DeviceStatusRefresh
    ) -> Bool {
        guard refresh.generation == generation else { return false }
        self.status = status
        syncStatus = .live
        return true
    }

    @discardableResult
    mutating func fail(for refresh: DeviceStatusRefresh) -> Bool {
        guard refresh.generation == generation else { return false }
        syncStatus = status == nil ? .offline : .stale
        return true
    }

    @discardableResult
    mutating func cancel(for refresh: DeviceStatusRefresh) -> Bool {
        guard refresh.generation == generation else { return false }
        if status == nil { syncStatus = .idle }
        return true
    }

    mutating func reset() {
        generation += 1
        status = nil
        syncStatus = .idle
    }
}
