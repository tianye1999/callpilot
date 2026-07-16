import Foundation

struct MessageWatermark: Codable, Equatable {
    let messageId: String
    let occurredAt: Int64
}

struct MessageCacheSnapshot: Codable, Equatable {
    let deviceId: String
    let messages: [SMSMessage]
    let watermark: MessageWatermark?
    let collectionRevision: String?
    let savedAt: Int64
}

@MainActor
protocol MessageCacheStoring: AnyObject {
    func load(deviceId: String) throws -> MessageCacheSnapshot?
    func save(_ snapshot: MessageCacheSnapshot) throws
    func clear() throws
}

@MainActor
final class FileMessageCacheStore: MessageCacheStoring {
    private let directoryURL: URL
    private var fileURL: URL { directoryURL.appendingPathComponent("messages-v1.json") }

    init(directoryURL: URL? = nil) {
        self.directoryURL = directoryURL ?? FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        )[0].appendingPathComponent("CallPilot", isDirectory: true)
    }

    func load(deviceId: String) throws -> MessageCacheSnapshot? {
        guard FileManager.default.fileExists(atPath: fileURL.path) else { return nil }
        let snapshot = try JSONDecoder().decode(
            MessageCacheSnapshot.self,
            from: Data(contentsOf: fileURL)
        )
        guard snapshot.deviceId == deviceId else {
            try clear()
            return nil
        }
        return snapshot
    }

    func save(_ snapshot: MessageCacheSnapshot) throws {
        try FileManager.default.createDirectory(
            at: directoryURL,
            withIntermediateDirectories: true
        )
        let data = try JSONEncoder().encode(snapshot)
        try data.write(
            to: fileURL,
            options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
        )
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
