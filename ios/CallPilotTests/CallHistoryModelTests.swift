import Foundation
import XCTest
@testable import CallPilot

@MainActor
final class CallHistoryModelTests: XCTestCase {
    func testRefreshLoadsListAndLateRevisionReplacesInPlaceWithoutDuplicate() async throws {
        let initial = try fixture(CallRecordsPage.self, "call-records-page.json")
        let readyDetail = try fixture(CallRecordDetail.self, "call-record-detail-ready.json")
        let pendingDetail = try fixture(CallRecordDetail.self, "call-record-detail-pending.json")
        let pending = CallRecordsPage(
            v: 1,
            items: [pendingDetail.record] + initial.items,
            nextCursor: nil,
            hasMore: false,
            collectionRevision: "revision_fixture_calls_0001",
            oldestAvailableAt: initial.oldestAvailableAt
        )
        let ready = CallRecordsPage(
            v: 1,
            items: [readyDetail.record] + initial.items,
            nextCursor: nil,
            hasMore: false,
            collectionRevision: "revision_fixture_calls_0002",
            oldestAvailableAt: initial.oldestAvailableAt
        )
        let client = FakeCallRecordContentClient(listResults: [.success(pending), .success(ready)])
        let model = CallHistoryModel(
            client: client,
            store: InMemoryCallHistoryCacheStore(),
            deviceId: "device_abcdefghijkl"
        )

        await model.refresh()
        let idsBefore = model.records.map(\.callId)
        await model.refresh()

        XCTAssertEqual(model.records.map(\.callId), idsBefore)
        XCTAssertEqual(Set(model.records.map(\.callId)).count, model.records.count)
        XCTAssertEqual(model.records[0].revision, readyDetail.record.revision)
        XCTAssertEqual(model.records[0].summaryState, .ready)
    }

    func testDetailMovesFromPendingToReadyWithoutLosingTimeline() async throws {
        let pending = try fixture(CallRecordDetail.self, "call-record-detail-pending.json")
        let ready = try fixture(CallRecordDetail.self, "call-record-detail-ready.json")
        let timeline = try fixture(CallTimelinePage.self, "call-timeline-page.json")
        let client = FakeCallRecordContentClient(
            detailResults: [.success(pending), .success(ready)],
            timelineResults: [.success(timeline), .success(timeline)]
        )
        let model = CallHistoryModel(
            client: client,
            store: InMemoryCallHistoryCacheStore(),
            deviceId: "device_abcdefghijkl"
        )

        await model.refreshDetail(callId: pending.record.callId)
        XCTAssertEqual(model.detail(for: pending.record.callId)?.detail?.record.summaryState, .pending)
        XCTAssertEqual(model.detail(for: pending.record.callId)?.timeline.count, 5)

        await model.refreshDetail(callId: pending.record.callId)

        let state = try XCTUnwrap(model.detail(for: pending.record.callId))
        XCTAssertEqual(state.detail?.record.summaryState, .ready)
        XCTAssertEqual(state.detail?.summary?.text, ready.summary?.text)
        XCTAssertEqual(state.timeline.count, 5)
        XCTAssertEqual(state.syncStatus, .live)
    }

    func testLateSummaryReplacesListAndSurvivesTimelineRefreshFailure() async throws {
        let pending = try fixture(CallRecordDetail.self, "call-record-detail-pending.json")
        let ready = try fixture(CallRecordDetail.self, "call-record-detail-ready.json")
        let list = CallRecordsPage(
            v: 1,
            items: [pending.record],
            nextCursor: nil,
            hasMore: false,
            collectionRevision: "revision_fixture_calls_0001",
            oldestAvailableAt: pending.record.startedAt
        )
        let store = InMemoryCallHistoryCacheStore()
        let client = FakeCallRecordContentClient(
            listResults: [.success(list)],
            detailResults: [.success(ready)],
            timelineResults: [.failure(URLError(.timedOut))]
        )
        let model = CallHistoryModel(
            client: client,
            store: store,
            deviceId: "device_abcdefghijkl"
        )

        await model.refresh()
        await model.refreshDetail(callId: pending.record.callId)

        XCTAssertEqual(model.records.first?.summaryState, .ready)
        XCTAssertEqual(model.records.first?.revision, ready.record.revision)
        XCTAssertEqual(model.detail(for: pending.record.callId)?.detail, ready)
        XCTAssertEqual(model.detail(for: pending.record.callId)?.syncStatus, .stale)
        XCTAssertEqual(store.snapshot?.details[pending.record.callId]?.detail, ready)
    }

    func testRemoteHandsetWithoutAIContentIsNormalEmptyState() async throws {
        let detail = try fixture(CallRecordDetail.self, "call-record-detail-no-transcript.json")
        let timeline = try fixture(CallTimelinePage.self, "call-timeline-empty.json")
        let client = FakeCallRecordContentClient(
            detailResults: [.success(detail)],
            timelineResults: [.success(timeline)]
        )
        let model = CallHistoryModel(
            client: client,
            store: InMemoryCallHistoryCacheStore(),
            deviceId: "device_abcdefghijkl"
        )

        await model.refreshDetail(callId: detail.record.callId)

        let state = try XCTUnwrap(model.detail(for: detail.record.callId))
        XCTAssertTrue(state.isNormalNoAIContent)
        XCTAssertNil(state.errorMessage)
        XCTAssertEqual(state.syncStatus, .live)
    }

    func testTimelinePaginationAppendsWithoutDuplicates() async throws {
        let detail = try fixture(CallRecordDetail.self, "call-record-detail-ready.json")
        let original = try fixture(CallTimelinePage.self, "call-timeline-page.json")
        let first = CallTimelinePage(
            v: 1,
            items: Array(original.items.prefix(3)),
            nextCursor: "cursor_fixture_timeline_0001",
            hasMore: true,
            collectionRevision: original.collectionRevision,
            oldestAvailableAt: original.oldestAvailableAt
        )
        let second = CallTimelinePage(
            v: 1,
            items: Array(original.items.suffix(3)),
            nextCursor: nil,
            hasMore: false,
            collectionRevision: original.collectionRevision,
            oldestAvailableAt: original.oldestAvailableAt
        )
        let client = FakeCallRecordContentClient(
            detailResults: [.success(detail)],
            timelineResults: [.success(first), .success(second)]
        )
        let model = CallHistoryModel(
            client: client,
            store: InMemoryCallHistoryCacheStore(),
            deviceId: "device_abcdefghijkl"
        )

        await model.refreshDetail(callId: detail.record.callId)
        await model.loadMoreTimeline(callId: detail.record.callId)

        let state = try XCTUnwrap(model.detail(for: detail.record.callId))
        XCTAssertEqual(state.timeline.count, original.items.count)
        XCTAssertFalse(state.hasMoreTimeline)
        XCTAssertEqual(Set(state.timeline.map(\.id)).count, state.timeline.count)
    }

    func testOfflineRefreshKeepsProtectedCacheStale() async throws {
        let page = try fixture(CallRecordsPage.self, "call-records-page.json")
        let store = InMemoryCallHistoryCacheStore(snapshot: CallHistoryCacheSnapshot(
            deviceId: "device_abcdefghijkl",
            records: page.items,
            collectionRevision: page.collectionRevision,
            details: [:],
            savedAt: 1_000
        ))
        let model = CallHistoryModel(
            client: FakeCallRecordContentClient(listResults: [.failure(URLError(.notConnectedToInternet))]),
            store: store,
            deviceId: "device_abcdefghijkl"
        )

        await model.refresh()

        XCTAssertEqual(model.records, page.items)
        XCTAssertEqual(model.syncStatus, .stale)
    }

    func testPayloadTooLargeUsesStableFallbackCopy() async {
        let model = CallHistoryModel(
            client: FakeCallRecordContentClient(listResults: [
                .failure(HostedCloudError(statusCode: 413, code: "PAYLOAD_TOO_LARGE", message: "server text")),
            ]),
            store: InMemoryCallHistoryCacheStore(),
            deviceId: "device_abcdefghijkl"
        )

        await model.refresh()

        XCTAssertEqual(model.errorCode, "PAYLOAD_TOO_LARGE")
        XCTAssertEqual(model.errorMessage, CallHistoryCopy.payloadTooLarge)
    }

    func testAuthorizationFailureAndClearFenceLateDetail() async throws {
        let page = try fixture(CallRecordsPage.self, "call-records-page.json")
        let detail = try fixture(CallRecordDetail.self, "call-record-detail-ready.json")
        let store = InMemoryCallHistoryCacheStore(snapshot: CallHistoryCacheSnapshot(
            deviceId: "device_abcdefghijkl",
            records: page.items,
            collectionRevision: page.collectionRevision,
            details: [:],
            savedAt: 1_000
        ))
        let client = SuspendedCallRecordContentClient()
        let model = CallHistoryModel(client: client, store: store, deviceId: "device_abcdefghijkl")
        let refresh = Task { await model.refreshDetail(callId: detail.record.callId) }
        while !client.isDetailWaiting { await Task.yield() }

        model.clearLocalData()
        client.succeedDetail(detail)
        await refresh.value

        XCTAssertTrue(model.records.isEmpty)
        XCTAssertNil(model.detail(for: detail.record.callId))
        XCTAssertNil(store.snapshot)
    }

    private func fixture<T: Decodable>(_ type: T.Type, _ name: String) throws -> T {
        try JSONDecoder().decode(type, from: ContentTestFixtures.data(named: name))
    }
}

@MainActor
private final class FakeCallRecordContentClient: CallRecordContentClient {
    var listResults: [Result<CallRecordsPage, Error>]
    var detailResults: [Result<CallRecordDetail, Error>]
    var timelineResults: [Result<CallTimelinePage, Error>]

    init(
        listResults: [Result<CallRecordsPage, Error>] = [],
        detailResults: [Result<CallRecordDetail, Error>] = [],
        timelineResults: [Result<CallTimelinePage, Error>] = []
    ) {
        self.listResults = listResults
        self.detailResults = detailResults
        self.timelineResults = timelineResults
    }

    func listCallRecords(limit: Int, cursor: String?) async throws -> CallRecordsPage {
        try listResults.removeFirst().get()
    }

    func getCallRecord(callId: String) async throws -> CallRecordDetail {
        try detailResults.removeFirst().get()
    }

    func listCallTimeline(callId: String, limit: Int, cursor: String?) async throws -> CallTimelinePage {
        try timelineResults.removeFirst().get()
    }
}

@MainActor
private final class InMemoryCallHistoryCacheStore: CallHistoryCacheStoring {
    var snapshot: CallHistoryCacheSnapshot?

    init(snapshot: CallHistoryCacheSnapshot? = nil) { self.snapshot = snapshot }

    func load(deviceId: String) throws -> CallHistoryCacheSnapshot? {
        snapshot?.deviceId == deviceId ? snapshot : nil
    }

    func save(_ snapshot: CallHistoryCacheSnapshot) throws { self.snapshot = snapshot }
    func clear() throws { snapshot = nil }
}

@MainActor
private final class SuspendedCallRecordContentClient: CallRecordContentClient {
    private var detailContinuation: CheckedContinuation<CallRecordDetail, Error>?
    var isDetailWaiting: Bool { detailContinuation != nil }

    func listCallRecords(limit: Int, cursor: String?) async throws -> CallRecordsPage {
        throw URLError(.unsupportedURL)
    }

    func getCallRecord(callId: String) async throws -> CallRecordDetail {
        try await withCheckedThrowingContinuation { detailContinuation = $0 }
    }

    func listCallTimeline(callId: String, limit: Int, cursor: String?) async throws -> CallTimelinePage {
        throw URLError(.unsupportedURL)
    }

    func succeedDetail(_ detail: CallRecordDetail) {
        detailContinuation?.resume(returning: detail)
        detailContinuation = nil
    }
}
