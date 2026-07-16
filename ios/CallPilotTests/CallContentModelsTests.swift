import Foundation
import XCTest
@testable import CallPilot

final class CallContentModelsTests: XCTestCase {
    func testCallRecordsPageDecodesFrozenSharedFixture() throws {
        let page = try decode(CallRecordsPage.self, fixture: "call-records-page.json")

        XCTAssertEqual(page.items.count, 2)
        XCTAssertEqual(page.items[0].callId, "call_fixture_agent_0001")
        XCTAssertEqual(page.items[0].summaryState, .ready)
        XCTAssertEqual(page.items[0].triageOutcome, .transferred)
        XCTAssertEqual(page.items[1].source, .remoteHandset)
        XCTAssertEqual(page.items[1].summaryState, .unavailable)
        XCTAssertFalse(page.items[1].hasTranscript)
    }

    func testPendingAndReadyFixturesKeepIdentityButAdvanceRevision() throws {
        let pending = try decode(CallRecordDetail.self, fixture: "call-record-detail-pending.json")
        let ready = try decode(CallRecordDetail.self, fixture: "call-record-detail-ready.json")

        XCTAssertEqual(pending.record.callId, ready.record.callId)
        XCTAssertNotEqual(pending.record.revision, ready.record.revision)
        XCTAssertEqual(pending.record.summaryState, .pending)
        XCTAssertNil(pending.summary)
        XCTAssertEqual(ready.record.summaryState, .ready)
        XCTAssertEqual(ready.summary?.ok, true)
        XCTAssertNotEqual(pending.timelineRevision, ready.timelineRevision)
    }

    func testRemoteHandsetFixtureIsValidWithoutAIContent() throws {
        let detail = try decode(CallRecordDetail.self, fixture: "call-record-detail-no-transcript.json")

        XCTAssertEqual(detail.record.source, .remoteHandset)
        XCTAssertEqual(detail.record.summaryState, .unavailable)
        XCTAssertFalse(detail.record.hasTranscript)
        XCTAssertNil(detail.summary)
    }

    func testCallDetailRejectsSummaryThatContradictsLifecycle() throws {
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: ContentTestFixtures.data(named: "call-record-detail-pending.json")
            ) as? [String: Any]
        )
        object["summary"] = [
            "ok": true, "text": "Synthetic summary", "callerIdentity": NSNull(),
            "intent": NSNull(), "urgency": NSNull(), "callbackNeeded": NSNull(),
            "errorCode": NSNull(), "resultSource": NSNull(), "resultVerification": NSNull(),
        ]

        XCTAssertThrowsError(
            try JSONDecoder().decode(
                CallRecordDetail.self,
                from: JSONSerialization.data(withJSONObject: object)
            )
        )
    }

    func testTimelineDecodesKnownUnionAndPreservesUnknownFutureItem() throws {
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: ContentTestFixtures.data(named: "call-timeline-page.json")
            ) as? [String: Any]
        )
        var items = try XCTUnwrap(object["items"] as? [[String: Any]])
        items.append([
            "timelineItemId": "item_fixture_future_0001",
            "occurredAt": 1_784_161_130_000,
            "type": "FUTURE_EVENT",
            "futureField": "ignored",
        ])
        object["items"] = items
        let page = try JSONDecoder().decode(
            CallTimelinePage.self,
            from: JSONSerialization.data(withJSONObject: object)
        )

        XCTAssertEqual(page.items.count, 6)
        XCTAssertEqual(page.items[0].kind, .transcript)
        XCTAssertEqual(page.items[2].kind, .triage)
        XCTAssertEqual(page.items[3].kind, .takeover)
        XCTAssertEqual(page.items[4].kind, .result)
        XCTAssertEqual(page.items[5].kind, .unknown)
        XCTAssertEqual(page.visibleItems.count, 5)
    }

    func testKnownTimelineTypeWithMissingRequiredFieldFailsClosed() throws {
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: ContentTestFixtures.data(named: "call-timeline-page.json")
            ) as? [String: Any]
        )
        var items = try XCTUnwrap(object["items"] as? [[String: Any]])
        items[0].removeValue(forKey: "text")
        object["items"] = items

        XCTAssertThrowsError(
            try JSONDecoder().decode(
                CallTimelinePage.self,
                from: JSONSerialization.data(withJSONObject: object)
            )
        )
    }

    func testOpaqueCursorAcceptsProtocolMaximumBeyondLegacyEightyCharacters() throws {
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: ContentTestFixtures.data(named: "call-records-page.json")
            ) as? [String: Any]
        )
        object["hasMore"] = true
        object["nextCursor"] = "cursor_" + String(repeating: "a", count: 256)

        let page = try JSONDecoder().decode(
            CallRecordsPage.self,
            from: JSONSerialization.data(withJSONObject: object)
        )

        XCTAssertEqual(page.nextCursor?.count, 263)
    }

    func testSummaryPresentationFollowsAllFourLifecycleStates() throws {
        let pending = try decode(CallRecordDetail.self, fixture: "call-record-detail-pending.json")
        let ready = try decode(CallRecordDetail.self, fixture: "call-record-detail-ready.json")
        let unavailable = try decode(CallRecordDetail.self, fixture: "call-record-detail-no-transcript.json")
        let failedRecord = CallRecordItem(
            callId: ready.record.callId,
            revision: ready.record.revision,
            direction: ready.record.direction,
            address: ready.record.address,
            startedAt: ready.record.startedAt,
            endedAt: ready.record.endedAt,
            durationMs: ready.record.durationMs,
            status: ready.record.status,
            answered: ready.record.answered,
            source: ready.record.source,
            summaryState: .failed,
            summaryPreview: nil,
            hasTranscript: ready.record.hasTranscript,
            triageOutcome: ready.record.triageOutcome
        )
        let failedSummary = CallSummary(
            ok: false,
            text: nil,
            callerIdentity: nil,
            intent: nil,
            urgency: nil,
            callbackNeeded: nil,
            errorCode: "MODEL_UNAVAILABLE",
            resultSource: nil,
            resultVerification: nil
        )
        let failed = CallRecordDetail(
            v: 1,
            record: failedRecord,
            summary: failedSummary,
            timelineRevision: ready.timelineRevision
        )

        XCTAssertEqual(CallSummaryPresentation(detail: unavailable), .hidden)
        XCTAssertEqual(CallSummaryPresentation(detail: pending), .pending)
        XCTAssertEqual(CallSummaryPresentation(detail: ready), .ready(ready.summary!))
        XCTAssertEqual(CallSummaryPresentation(detail: failed), .failed(failedSummary))
    }

    private func decode<T: Decodable>(_ type: T.Type, fixture: String) throws -> T {
        try JSONDecoder().decode(type, from: ContentTestFixtures.data(named: fixture))
    }
}
