import Foundation
import XCTest
@testable import CallPilot

@MainActor
final class MessageInboxModelTests: XCTestCase {
    func testSuccessfulRefreshIsLiveAndAdvancesWatermarkOnlyAfterDisplay() async throws {
        let page = try fixturePage()
        let client = FakeMessageContentClient(results: [.success(page)])
        let store = InMemoryMessageCacheStore()
        let model = MessageInboxModel(
            client: client,
            store: store,
            deviceId: "device_abcdefghijkl",
            clockMilliseconds: { 2_000 }
        )

        await model.refresh()

        XCTAssertEqual(model.syncStatus, .live)
        XCTAssertEqual(model.messages, page.items)
        XCTAssertEqual(model.unreadCount, 3)
        XCTAssertNil(store.snapshot?.watermark)

        model.setVisible(true)
        model.markLatestDisplayed()

        XCTAssertEqual(model.unreadCount, 0)
        XCTAssertEqual(store.snapshot?.watermark?.messageId, page.items[0].messageId)
    }

    func testFailedRefreshKeepsCachedMessagesStaleAndDoesNotAdvanceWatermark() async throws {
        let page = try fixturePage()
        let watermark = MessageWatermark(messageId: page.items[2].messageId, occurredAt: page.items[2].occurredAt)
        let store = InMemoryMessageCacheStore(snapshot: MessageCacheSnapshot(
            deviceId: "device_abcdefghijkl",
            messages: page.items,
            watermark: watermark,
            collectionRevision: page.collectionRevision,
            savedAt: 1_000
        ))
        let client = FakeMessageContentClient(results: [.failure(URLError(.notConnectedToInternet))])
        let model = MessageInboxModel(client: client, store: store, deviceId: "device_abcdefghijkl")
        model.setVisible(true)

        await model.refresh()
        model.markLatestDisplayed()

        XCTAssertEqual(model.syncStatus, .stale)
        XCTAssertEqual(model.messages, page.items)
        XCTAssertEqual(model.unreadCount, 2)
        XCTAssertEqual(store.snapshot?.watermark, watermark)
    }

    func testFailedRefreshWithoutCacheIsOffline() async {
        let client = FakeMessageContentClient(results: [.failure(URLError(.notConnectedToInternet))])
        let model = MessageInboxModel(
            client: client,
            store: InMemoryMessageCacheStore(),
            deviceId: "device_abcdefghijkl"
        )

        await model.refresh()

        XCTAssertEqual(model.syncStatus, .offline)
        XCTAssertTrue(model.messages.isEmpty)
    }

    func testPayloadTooLargeUsesStableFallbackCopy() async {
        let client = FakeMessageContentClient(results: [
            .failure(HostedCloudError(
                statusCode: 413,
                code: "PAYLOAD_TOO_LARGE",
                message: "server text must not be shown"
            )),
        ])
        let model = MessageInboxModel(
            client: client,
            store: InMemoryMessageCacheStore(),
            deviceId: "device_abcdefghijkl"
        )

        await model.refresh()

        XCTAssertEqual(model.errorCode, "PAYLOAD_TOO_LARGE")
        XCTAssertEqual(model.errorMessage, MessageInboxCopy.payloadTooLarge)
    }

    func testAuthorizationFailureClearsCachedContentAndWatermark() async throws {
        let page = try fixturePage()
        let store = InMemoryMessageCacheStore(snapshot: MessageCacheSnapshot(
            deviceId: "device_abcdefghijkl",
            messages: page.items,
            watermark: MessageWatermark(messageId: page.items[0].messageId, occurredAt: page.items[0].occurredAt),
            collectionRevision: page.collectionRevision,
            savedAt: 1_000
        ))
        let client = FakeMessageContentClient(results: [
            .failure(HostedCloudError(statusCode: 401, code: "UNAUTHORIZED", message: "revoked")),
        ])
        var didRevoke = false
        let model = MessageInboxModel(
            client: client,
            store: store,
            deviceId: "device_abcdefghijkl",
            onUnauthorized: { didRevoke = true }
        )

        await model.refresh()

        XCTAssertTrue(model.messages.isEmpty)
        XCTAssertNil(store.snapshot)
        XCTAssertEqual(store.clearCount, 1)
        XCTAssertTrue(didRevoke)
    }

    func testLoadMoreAppendsWithoutDuplicatingExistingMessages() async throws {
        let first = try fixturePage()
        let extra = SMSMessage(
            messageId: "msg_fixture_older_0001",
            revision: "revision_message_older_0001",
            direction: .inbound,
            address: "fixture-address",
            text: "Synthetic older notice.",
            occurredAt: 1_784_100_000_000,
            recordedAt: 1_784_100_000_100,
            status: .received
        )
        let second = MessagePage(
            v: 1,
            items: [first.items[2], extra],
            nextCursor: nil,
            hasMore: false,
            collectionRevision: first.collectionRevision,
            oldestAvailableAt: extra.occurredAt
        )
        let client = FakeMessageContentClient(results: [.success(first), .success(second)])
        let model = MessageInboxModel(
            client: client,
            store: InMemoryMessageCacheStore(),
            deviceId: "device_abcdefghijkl"
        )

        await model.refresh()
        await model.loadMore()

        XCTAssertEqual(model.messages.count, 4)
        XCTAssertEqual(model.messages.last, extra)
        XCTAssertFalse(model.hasMore)
        XCTAssertEqual(client.requests.map(\.cursor), [nil, first.nextCursor])
    }

    func testPaginationKeepsMessageBeyondFirstHundredVisible() async {
        let messages = (0...100).map(makeBulkMessage)
        let first = MessagePage(
            v: 1,
            items: Array(messages.prefix(100)),
            nextCursor: "cursor_fixture_bulk_0001",
            hasMore: true,
            collectionRevision: "revision_fixture_bulk_0001",
            oldestAvailableAt: messages.last?.occurredAt
        )
        let second = MessagePage(
            v: 1,
            items: [messages[100]],
            nextCursor: nil,
            hasMore: false,
            collectionRevision: "revision_fixture_bulk_0001",
            oldestAvailableAt: messages.last?.occurredAt
        )
        let store = InMemoryMessageCacheStore()
        let model = MessageInboxModel(
            client: FakeMessageContentClient(results: [.success(first), .success(second)]),
            store: store,
            deviceId: "device_abcdefghijkl"
        )

        await model.refresh()
        await model.loadMore()

        XCTAssertEqual(model.messages.count, 101)
        XCTAssertEqual(model.messages.last, messages[100])
        XCTAssertFalse(model.hasMore)
        XCTAssertEqual(store.snapshot?.messages.count, 101)
    }

    func testLoadingCacheKeepsAtMostEdgeRetentionWindow() async {
        let cached = (0...500).map(makeBulkMessage)
        let store = InMemoryMessageCacheStore(snapshot: MessageCacheSnapshot(
            deviceId: "device_abcdefghijkl",
            messages: cached,
            watermark: nil,
            collectionRevision: "revision_fixture_bulk_0001",
            savedAt: 1_000
        ))
        let model = MessageInboxModel(
            client: FakeMessageContentClient(results: [.failure(URLError(.notConnectedToInternet))]),
            store: store,
            deviceId: "device_abcdefghijkl"
        )

        await model.refresh()

        XCTAssertEqual(model.messages.count, 500)
        XCTAssertEqual(model.messages.last, cached[499])
        XCTAssertEqual(model.syncStatus, .stale)
        XCTAssertEqual(store.snapshot?.messages.count, 500)
    }

    func testFileCacheIsBoundToDeviceAndRoundTripsWatermark() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let page = try fixturePage()
        let store = FileMessageCacheStore(directoryURL: directory)
        let snapshot = MessageCacheSnapshot(
            deviceId: "device_abcdefghijkl",
            messages: page.items,
            watermark: MessageWatermark(messageId: page.items[0].messageId, occurredAt: page.items[0].occurredAt),
            collectionRevision: page.collectionRevision,
            savedAt: 1_000
        )

        try store.save(snapshot)

        XCTAssertEqual(try store.load(deviceId: "device_abcdefghijkl"), snapshot)
        XCTAssertNil(try store.load(deviceId: "device_otherdevice12"))
    }

    func testClearFencesLateRefreshResponseFromRepopulatingCache() async throws {
        let page = try fixturePage()
        let client = SuspendedMessageContentClient()
        let store = InMemoryMessageCacheStore()
        let model = MessageInboxModel(
            client: client,
            store: store,
            deviceId: "device_abcdefghijkl"
        )
        let refresh = Task { await model.refresh() }
        while !client.isWaiting { await Task.yield() }

        model.clearLocalData()
        client.succeed(with: page)
        await refresh.value

        XCTAssertTrue(model.messages.isEmpty)
        XCTAssertNil(store.snapshot)
        XCTAssertEqual(model.syncStatus, .idle)
    }

    func testSettingsCanLoadProtectedCacheWithoutStartingNetworkRefresh() throws {
        let page = try fixturePage()
        let store = InMemoryMessageCacheStore(snapshot: MessageCacheSnapshot(
            deviceId: "device_abcdefghijkl",
            messages: page.items,
            watermark: nil,
            collectionRevision: page.collectionRevision,
            savedAt: 1_000
        ))
        let client = FakeMessageContentClient(results: [])
        let model = MessageInboxModel(client: client, store: store, deviceId: "device_abcdefghijkl")

        model.loadCachedContent()

        XCTAssertEqual(model.messages, page.items)
        XCTAssertEqual(model.syncStatus, .stale)
        XCTAssertTrue(client.requests.isEmpty)
    }

    private func fixturePage() throws -> MessagePage {
        try JSONDecoder().decode(
            MessagePage.self,
            from: ContentTestFixtures.data(named: "messages-page.json")
        )
    }

    private func makeBulkMessage(_ index: Int) -> SMSMessage {
        SMSMessage(
            messageId: String(format: "msg_fixture_bulk_%04d", index),
            revision: String(format: "revision_fixture_bulk_%04d", index),
            direction: .inbound,
            address: "fixture-address",
            text: "Synthetic message \(index).",
            occurredAt: 2_000_000 - Int64(index),
            recordedAt: 2_000_000 - Int64(index),
            status: .received
        )
    }
}

@MainActor
private final class FakeMessageContentClient: MessageContentClient {
    struct Request: Equatable {
        let limit: Int
        let cursor: String?
    }

    var results: [Result<MessagePage, Error>]
    private(set) var requests: [Request] = []

    init(results: [Result<MessagePage, Error>]) {
        self.results = results
    }

    func listMessages(limit: Int, cursor: String?) async throws -> MessagePage {
        requests.append(Request(limit: limit, cursor: cursor))
        return try results.removeFirst().get()
    }
}

@MainActor
private final class InMemoryMessageCacheStore: MessageCacheStoring {
    var snapshot: MessageCacheSnapshot?
    private(set) var clearCount = 0

    init(snapshot: MessageCacheSnapshot? = nil) {
        self.snapshot = snapshot
    }

    func load(deviceId: String) throws -> MessageCacheSnapshot? {
        guard snapshot?.deviceId == deviceId else { return nil }
        return snapshot
    }

    func save(_ snapshot: MessageCacheSnapshot) throws {
        self.snapshot = snapshot
    }

    func clear() throws {
        snapshot = nil
        clearCount += 1
    }
}

@MainActor
private final class SuspendedMessageContentClient: MessageContentClient {
    private var continuation: CheckedContinuation<MessagePage, Error>?
    var isWaiting: Bool { continuation != nil }

    func listMessages(limit: Int, cursor: String?) async throws -> MessagePage {
        try await withCheckedThrowingContinuation { continuation = $0 }
    }

    func succeed(with page: MessagePage) {
        continuation?.resume(returning: page)
        continuation = nil
    }
}
