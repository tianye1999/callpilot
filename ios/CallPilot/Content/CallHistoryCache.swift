import Foundation

struct CachedCallDetail: Codable, Equatable {
    let detail: CallRecordDetail
    let timeline: [CallTimelineItem]
    let nextTimelineCursor: String?
    let timelineCollectionRevision: String?
}

struct CallHistoryCacheSnapshot: Codable, Equatable {
    let deviceId: String
    let records: [CallRecordItem]
    let collectionRevision: String?
    let details: [String: CachedCallDetail]
    let savedAt: Int64
}

@MainActor
protocol CallHistoryCacheStoring: AnyObject {
    func load(deviceId: String) throws -> CallHistoryCacheSnapshot?
    func save(_ snapshot: CallHistoryCacheSnapshot) throws
    func clear() throws
}

@MainActor
final class FileCallHistoryCacheStore: CallHistoryCacheStoring {
    private let directoryURL: URL
    private var fileURL: URL { directoryURL.appendingPathComponent("call-history-v1.json") }

    init(directoryURL: URL? = nil) {
        self.directoryURL = directoryURL ?? FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        )[0].appendingPathComponent("CallPilot", isDirectory: true)
    }

    func load(deviceId: String) throws -> CallHistoryCacheSnapshot? {
        guard FileManager.default.fileExists(atPath: fileURL.path) else { return nil }
        let snapshot = try JSONDecoder().decode(
            CallHistoryCacheSnapshot.self,
            from: Data(contentsOf: fileURL)
        )
        guard snapshot.deviceId == deviceId else {
            try clear()
            return nil
        }
        return snapshot
    }

    func save(_ snapshot: CallHistoryCacheSnapshot) throws {
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
        let data = try JSONEncoder().encode(snapshot)
        try data.write(to: fileURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var protectedFile = fileURL
        try protectedFile.setResourceValues(values)
    }

    func clear() throws {
        guard FileManager.default.fileExists(atPath: fileURL.path) else { return }
        try FileManager.default.removeItem(at: fileURL)
    }
}
