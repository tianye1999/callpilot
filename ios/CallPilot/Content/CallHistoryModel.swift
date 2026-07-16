import Combine
import Foundation

enum CallHistorySyncStatus: Equatable {
    case idle
    case loading
    case live
    case stale
    case offline
}

enum CallHistoryCopy {
    static var payloadTooLarge: String { L10n.text("calls.error.payload_too_large") }
    static var unavailable: String { L10n.text("calls.error.unavailable") }
    static var edgeOffline: String { L10n.text("calls.error.edge_offline") }
    static var featureDisabled: String { L10n.text("calls.error.feature_disabled") }
    static var unauthorized: String { L10n.text("content.error.unauthorized") }
}

enum CallSummaryPresentation: Equatable {
    case hidden
    case pending
    case ready(CallSummary?)
    case failed(CallSummary?)

    init(detail: CallRecordDetail) {
        switch detail.record.summaryState {
        case .unavailable:
            self = .hidden
        case .pending:
            self = .pending
        case .ready:
            self = .ready(detail.summary)
        case .failed:
            self = .failed(detail.summary)
        }
    }
}

struct CallDetailState: Equatable {
    var detail: CallRecordDetail?
    var timeline: [CallTimelineItem]
    var nextTimelineCursor: String?
    var timelineCollectionRevision: String?
    var syncStatus: CallHistorySyncStatus
    var errorCode: String?
    var errorMessage: String?
    var isLoadingMore: Bool

    var hasMoreTimeline: Bool { nextTimelineCursor != nil }
    var visibleTimeline: [CallTimelineItem] { timeline.filter { $0.kind != .unknown } }
    var isNormalNoAIContent: Bool {
        detail?.record.source == .remoteHandset
            && detail?.record.summaryState == .unavailable
            && detail?.record.hasTranscript == false
            && visibleTimeline.isEmpty
    }
}

@MainActor
final class CallHistoryModel: ObservableObject {
    private static let maxCachedRecords = 500
    private static let maxCachedDetails = 50
    private static let maxTimelineItems = 500

    @Published private(set) var records: [CallRecordItem] = []
    @Published private(set) var syncStatus: CallHistorySyncStatus = .idle
    @Published private(set) var errorCode: String?
    @Published private(set) var errorMessage: String?
    @Published private(set) var isRefreshing = false
    @Published private(set) var isLoadingMore = false
    @Published private(set) var collectionRevision: String?
    @Published private(set) var details: [String: CallDetailState] = [:]

    private let client: CallRecordContentClient
    private let store: CallHistoryCacheStoring
    private let deviceId: String
    private let clockMilliseconds: () -> Int64
    private let onUnauthorized: @MainActor () -> Void
    private var nextCursor: String?
    private var didLoadCache = false
    private var generation = 0
    private var loadingDetails = Set<String>()

    var hasMore: Bool { nextCursor != nil }

    init(
        client: CallRecordContentClient,
        store: CallHistoryCacheStoring,
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

    func detail(for callId: String) -> CallDetailState? { details[callId] }

    func loadCachedContent() {
        loadCacheIfNeeded()
    }

    func refresh() async {
        guard !isRefreshing, !isLoadingMore else { return }
        loadCacheIfNeeded()
        let requestGeneration = generation
        isRefreshing = true
        if records.isEmpty { syncStatus = .loading }
        defer { isRefreshing = false }
        do {
            let page = try await client.listCallRecords(limit: 25, cursor: nil)
            guard requestGeneration == generation else { return }
            let freshIds = Set(page.items.map(\.callId))
            records = Array(
                (page.items + records.filter { !freshIds.contains($0.callId) })
                    .prefix(Self.maxCachedRecords)
            )
            nextCursor = page.nextCursor
            collectionRevision = page.collectionRevision
            syncStatus = .live
            errorCode = nil
            errorMessage = nil
            saveCache()
        } catch {
            guard requestGeneration == generation else { return }
            handleListError(error)
        }
    }

    func loadMore() async {
        guard let cursor = nextCursor, !isRefreshing, !isLoadingMore else { return }
        let requestGeneration = generation
        isLoadingMore = true
        defer { isLoadingMore = false }
        do {
            let page = try await client.listCallRecords(limit: 25, cursor: cursor)
            guard requestGeneration == generation else { return }
            var indexes = Dictionary(uniqueKeysWithValues: records.enumerated().map { ($1.callId, $0) })
            for record in page.items {
                if let index = indexes[record.callId] {
                    records[index] = record
                } else {
                    indexes[record.callId] = records.count
                    records.append(record)
                }
            }
            records = Array(records.prefix(Self.maxCachedRecords))
            nextCursor = page.nextCursor
            collectionRevision = page.collectionRevision
            syncStatus = .live
            errorCode = nil
            errorMessage = nil
            saveCache()
        } catch {
            guard requestGeneration == generation else { return }
            handleListError(error)
        }
    }

    func refreshDetail(callId: String) async {
        guard !loadingDetails.contains(callId) else { return }
        loadCacheIfNeeded()
        let requestGeneration = generation
        loadingDetails.insert(callId)
        var state = details[callId] ?? Self.emptyDetailState
        if state.detail == nil { state.syncStatus = .loading }
        details[callId] = state
        defer { loadingDetails.remove(callId) }

        do {
            let detail = try await client.getCallRecord(callId: callId)
            guard requestGeneration == generation else { return }
            guard detail.record.callId == callId else { throw Self.invalidResponseError }
            state = details[callId] ?? Self.emptyDetailState
            state.detail = detail
            state.errorCode = nil
            state.errorMessage = nil
            details[callId] = state
            replaceListRecord(detail.record)
            saveCache()

            let timeline = try await client.listCallTimeline(callId: callId, limit: 50, cursor: nil)
            guard requestGeneration == generation else { return }
            state = details[callId] ?? Self.emptyDetailState
            state.timeline = Array(timeline.items.prefix(Self.maxTimelineItems))
            state.nextTimelineCursor = timeline.nextCursor
            state.timelineCollectionRevision = timeline.collectionRevision
            state.syncStatus = .live
            state.errorCode = nil
            state.errorMessage = nil
            details[callId] = state
            saveCache()
        } catch {
            guard requestGeneration == generation else { return }
            handleDetailError(error, callId: callId)
        }
    }

    func loadMoreTimeline(callId: String) async {
        guard var state = details[callId],
              let cursor = state.nextTimelineCursor,
              !state.isLoadingMore else { return }
        let requestGeneration = generation
        state.isLoadingMore = true
        details[callId] = state
        defer {
            if var latest = details[callId] {
                latest.isLoadingMore = false
                details[callId] = latest
            }
        }
        do {
            let page = try await client.listCallTimeline(callId: callId, limit: 50, cursor: cursor)
            guard requestGeneration == generation else { return }
            state = details[callId] ?? Self.emptyDetailState
            var indexes = Dictionary(uniqueKeysWithValues: state.timeline.enumerated().map { ($1.id, $0) })
            for item in page.items {
                if let index = indexes[item.id] {
                    state.timeline[index] = item
                } else {
                    indexes[item.id] = state.timeline.count
                    state.timeline.append(item)
                }
            }
            state.timeline = Array(state.timeline.prefix(Self.maxTimelineItems))
            state.nextTimelineCursor = page.nextCursor
            state.timelineCollectionRevision = page.collectionRevision
            state.syncStatus = .live
            state.errorCode = nil
            state.errorMessage = nil
            details[callId] = state
            saveCache()
        } catch {
            guard requestGeneration == generation else { return }
            handleDetailError(error, callId: callId)
        }
    }

    func clearLocalData() {
        generation += 1
        try? store.clear()
        records = []
        details = [:]
        nextCursor = nil
        collectionRevision = nil
        errorCode = nil
        errorMessage = nil
        syncStatus = .idle
        didLoadCache = true
    }

    private func loadCacheIfNeeded() {
        guard !didLoadCache else { return }
        didLoadCache = true
        guard let snapshot = try? store.load(deviceId: deviceId) else { return }
        records = Array(snapshot.records.prefix(Self.maxCachedRecords))
        collectionRevision = snapshot.collectionRevision
        details = snapshot.details.mapValues { cached in
            CallDetailState(
                detail: cached.detail,
                timeline: Array(cached.timeline.prefix(Self.maxTimelineItems)),
                nextTimelineCursor: cached.nextTimelineCursor,
                timelineCollectionRevision: cached.timelineCollectionRevision,
                syncStatus: .stale,
                errorCode: nil,
                errorMessage: nil,
                isLoadingMore: false
            )
        }
        if !records.isEmpty { syncStatus = .stale }
        if snapshot.records.count > Self.maxCachedRecords
            || snapshot.details.count > Self.maxCachedDetails
            || snapshot.details.values.contains(where: { $0.timeline.count > Self.maxTimelineItems }) {
            saveCache()
        }
    }

    private func replaceListRecord(_ record: CallRecordItem) {
        guard let index = records.firstIndex(where: { $0.callId == record.callId }) else { return }
        records[index] = record
    }

    private func handleListError(_ error: Error) {
        let hosted = error as? HostedCloudError
        if hosted?.code == "UNAUTHORIZED" {
            clearLocalData()
            errorCode = "UNAUTHORIZED"
            errorMessage = CallHistoryCopy.unauthorized
            syncStatus = .offline
            onUnauthorized()
            return
        }
        errorCode = hosted?.code
        errorMessage = Self.copy(for: hosted?.code)
        syncStatus = records.isEmpty ? .offline : .stale
    }

    private func handleDetailError(_ error: Error, callId: String) {
        let hosted = error as? HostedCloudError
        if hosted?.code == "UNAUTHORIZED" {
            clearLocalData()
            errorCode = "UNAUTHORIZED"
            errorMessage = CallHistoryCopy.unauthorized
            syncStatus = .offline
            onUnauthorized()
            return
        }
        var state = details[callId] ?? Self.emptyDetailState
        state.errorCode = hosted?.code
        state.errorMessage = Self.copy(for: hosted?.code)
        state.syncStatus = state.detail == nil ? .offline : .stale
        details[callId] = state
    }

    private func saveCache() {
        let allowedDetailIds = Set(records.prefix(Self.maxCachedDetails).map(\.callId))
        let cachedDetails = details.compactMapValues { state -> CachedCallDetail? in
            guard allowedDetailIds.contains(state.detail?.record.callId ?? ""),
                  let detail = state.detail else { return nil }
            return CachedCallDetail(
                detail: detail,
                timeline: Array(state.timeline.prefix(Self.maxTimelineItems)),
                nextTimelineCursor: state.nextTimelineCursor,
                timelineCollectionRevision: state.timelineCollectionRevision
            )
        }
        try? store.save(CallHistoryCacheSnapshot(
            deviceId: deviceId,
            records: Array(records.prefix(Self.maxCachedRecords)),
            collectionRevision: collectionRevision,
            details: cachedDetails,
            savedAt: clockMilliseconds()
        ))
    }

    private static func copy(for code: String?) -> String {
        switch code {
        case "PAYLOAD_TOO_LARGE": CallHistoryCopy.payloadTooLarge
        case "EDGE_OFFLINE", "TIMEOUT": CallHistoryCopy.edgeOffline
        case "FEATURE_DISABLED", "FORBIDDEN": CallHistoryCopy.featureDisabled
        case "UNAUTHORIZED": CallHistoryCopy.unauthorized
        default: CallHistoryCopy.unavailable
        }
    }

    private static let emptyDetailState = CallDetailState(
        detail: nil, timeline: [], nextTimelineCursor: nil,
        timelineCollectionRevision: nil, syncStatus: .idle,
        errorCode: nil, errorMessage: nil, isLoadingMore: false
    )
    private static let invalidResponseError = HostedCloudError(
        statusCode: 200,
        code: "INVALID_RESPONSE",
        message: L10n.text("calls.error.identity_mismatch")
    )
}
