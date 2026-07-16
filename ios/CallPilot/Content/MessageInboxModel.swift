import Combine
import Foundation

enum MessageSyncStatus: Equatable {
    case idle
    case loading
    case live
    case stale
    case offline
}

enum MessageInboxCopy {
    static var payloadTooLarge: String { L10n.text("messages.error.payload_too_large") }
    static var unavailable: String { L10n.text("messages.error.unavailable") }
    static var edgeOffline: String { L10n.text("messages.error.edge_offline") }
    static var featureDisabled: String { L10n.text("messages.error.feature_disabled") }
    static var unauthorized: String { L10n.text("content.error.unauthorized") }
}

@MainActor
final class MessageInboxModel: ObservableObject {
    private static let maxCachedMessages = 500

    @Published private(set) var messages: [SMSMessage] = []
    @Published private(set) var syncStatus: MessageSyncStatus = .idle
    @Published private(set) var unreadCount = 0
    @Published private(set) var errorCode: String?
    @Published private(set) var errorMessage: String?
    @Published private(set) var isRefreshing = false
    @Published private(set) var isLoadingMore = false
    @Published private(set) var collectionRevision: String?

    private let client: MessageContentClient
    private let store: MessageCacheStoring
    private let deviceId: String
    private let clockMilliseconds: () -> Int64
    private let onUnauthorized: @MainActor () -> Void
    private var watermark: MessageWatermark?
    private var nextCursor: String?
    private var didLoadCache = false
    private var isVisible = false
    private var generation = 0

    var hasMore: Bool { nextCursor != nil }

    init(
        client: MessageContentClient,
        store: MessageCacheStoring,
        deviceId: String,
        clockMilliseconds: @escaping () -> Int64 = {
            Int64(Date().timeIntervalSince1970 * 1_000)
        },
        onUnauthorized: @escaping @MainActor () -> Void = {}
    ) {
        self.client = client
        self.store = store
        self.deviceId = deviceId
        self.clockMilliseconds = clockMilliseconds
        self.onUnauthorized = onUnauthorized
    }

    func refresh() async {
        guard !isRefreshing, !isLoadingMore else { return }
        loadCacheIfNeeded()
        let requestGeneration = generation
        isRefreshing = true
        if messages.isEmpty { syncStatus = .loading }
        defer { isRefreshing = false }

        do {
            let page = try await client.listMessages(limit: 25, cursor: nil)
            guard requestGeneration == generation else { return }
            messages = mergeFirstPage(page.items, cached: messages)
            nextCursor = page.nextCursor
            collectionRevision = page.collectionRevision
            syncStatus = .live
            errorCode = nil
            errorMessage = nil
            recomputeUnreadCount()
            saveCache()
        } catch {
            guard requestGeneration == generation else { return }
            handle(error)
        }
    }

    func loadCachedContent() {
        loadCacheIfNeeded()
    }

    func loadMore() async {
        guard let cursor = nextCursor, !isRefreshing, !isLoadingMore else { return }
        let requestGeneration = generation
        isLoadingMore = true
        defer { isLoadingMore = false }
        do {
            let page = try await client.listMessages(limit: 25, cursor: cursor)
            guard requestGeneration == generation else { return }
            var indexes = Dictionary(uniqueKeysWithValues: messages.enumerated().map { ($1.messageId, $0) })
            for item in page.items {
                if let index = indexes[item.messageId] {
                    messages[index] = item
                } else {
                    indexes[item.messageId] = messages.count
                    messages.append(item)
                }
            }
            messages = Array(messages.prefix(Self.maxCachedMessages))
            nextCursor = page.nextCursor
            collectionRevision = page.collectionRevision
            syncStatus = .live
            errorCode = nil
            errorMessage = nil
            recomputeUnreadCount()
            saveCache()
        } catch {
            guard requestGeneration == generation else { return }
            handle(error)
        }
    }

    func setVisible(_ visible: Bool) {
        isVisible = visible
    }

    func markLatestDisplayed() {
        guard isVisible, syncStatus == .live, let latest = messages.first else { return }
        watermark = MessageWatermark(messageId: latest.messageId, occurredAt: latest.occurredAt)
        unreadCount = 0
        saveCache()
    }

    func clearLocalData() {
        generation += 1
        try? store.clear()
        messages = []
        watermark = nil
        nextCursor = nil
        collectionRevision = nil
        unreadCount = 0
        errorCode = nil
        errorMessage = nil
        syncStatus = .idle
        didLoadCache = true
    }

    private func loadCacheIfNeeded() {
        guard !didLoadCache else { return }
        didLoadCache = true
        guard let snapshot = try? store.load(deviceId: deviceId) else { return }
        messages = Array(snapshot.messages.prefix(Self.maxCachedMessages))
        watermark = snapshot.watermark
        collectionRevision = snapshot.collectionRevision
        if !messages.isEmpty { syncStatus = .stale }
        recomputeUnreadCount()
        if snapshot.messages.count > Self.maxCachedMessages {
            saveCache()
        }
    }

    private func mergeFirstPage(_ fresh: [SMSMessage], cached: [SMSMessage]) -> [SMSMessage] {
        let freshIds = Set(fresh.map(\.messageId))
        return Array(
            (fresh + cached.filter { !freshIds.contains($0.messageId) })
                .prefix(Self.maxCachedMessages)
        )
    }

    private func recomputeUnreadCount() {
        guard let watermark else {
            unreadCount = messages.count
            return
        }
        if let index = messages.firstIndex(where: { $0.messageId == watermark.messageId }) {
            unreadCount = index
            return
        }
        unreadCount = messages.prefix { message in
            message.occurredAt > watermark.occurredAt
                || (message.occurredAt == watermark.occurredAt && message.messageId > watermark.messageId)
        }.count
    }

    private func handle(_ error: Error) {
        let hostedError = error as? HostedCloudError
        errorCode = hostedError?.code
        switch hostedError?.code {
        case "UNAUTHORIZED":
            clearLocalData()
            errorCode = "UNAUTHORIZED"
            errorMessage = MessageInboxCopy.unauthorized
            syncStatus = .offline
            onUnauthorized()
            return
        case "PAYLOAD_TOO_LARGE":
            errorMessage = MessageInboxCopy.payloadTooLarge
        case "EDGE_OFFLINE", "TIMEOUT":
            errorMessage = MessageInboxCopy.edgeOffline
        case "FEATURE_DISABLED", "FORBIDDEN":
            errorMessage = MessageInboxCopy.featureDisabled
        default:
            errorMessage = MessageInboxCopy.unavailable
        }
        syncStatus = messages.isEmpty ? .offline : .stale
    }

    private func saveCache() {
        try? store.save(MessageCacheSnapshot(
            deviceId: deviceId,
            messages: Array(messages.prefix(Self.maxCachedMessages)),
            watermark: watermark,
            collectionRevision: collectionRevision,
            savedAt: clockMilliseconds()
        ))
    }
}
