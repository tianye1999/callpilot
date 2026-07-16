import Foundation
import XCTest
@testable import CallPilot

final class ContentModelsTests: XCTestCase {
    func testMessagesPageDecodesFrozenSharedFixture() throws {
        let page = try JSONDecoder().decode(
            MessagePage.self,
            from: ContentTestFixtures.data(named: "messages-page.json")
        )

        XCTAssertEqual(page.v, 1)
        XCTAssertEqual(page.items.count, 3)
        XCTAssertEqual(page.items.first?.messageId, "msg_fixture_outbound_0001")
        XCTAssertEqual(page.items.first?.direction, .outbound)
        XCTAssertEqual(page.items.first?.status, .sent)
        XCTAssertEqual(page.nextCursor, "cursor_messages_fixture_0001")
        XCTAssertTrue(page.hasMore)
        XCTAssertEqual(page.collectionRevision, "revision_messages_fixture_0001")
        XCTAssertEqual(page.oldestAvailableAt, 1_784_100_000_000)

        let fragments = page.items.filter { $0.occurredAt == 1_784_160_200_000 }
        XCTAssertEqual(fragments.count, 2)
        XCTAssertNotEqual(fragments[0].messageId, fragments[1].messageId)
    }

    func testMessagesPageIgnoresUnknownFields() throws {
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: ContentTestFixtures.data(named: "messages-page.json")
            ) as? [String: Any]
        )
        object["futureEnvelopeField"] = "ignored"
        var items = try XCTUnwrap(object["items"] as? [[String: Any]])
        items[0]["futureItemField"] = ["nested": true]
        object["items"] = items

        let page = try JSONDecoder().decode(
            MessagePage.self,
            from: JSONSerialization.data(withJSONObject: object)
        )

        XCTAssertEqual(page.items.count, 3)
    }

    func testMessagesPageRejectsCursorInvariantViolation() throws {
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: ContentTestFixtures.data(named: "messages-page.json")
            ) as? [String: Any]
        )
        object["nextCursor"] = NSNull()

        XCTAssertThrowsError(
            try JSONDecoder().decode(
                MessagePage.self,
                from: JSONSerialization.data(withJSONObject: object)
            )
        )
    }

    func testMessagesPageRejectsInvalidMessageIdentityAndBooleanTimestamp() throws {
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: ContentTestFixtures.data(named: "messages-page.json")
            ) as? [String: Any]
        )
        var items = try XCTUnwrap(object["items"] as? [[String: Any]])
        items[0]["messageId"] = "local-file-name"
        items[0]["occurredAt"] = true
        object["items"] = items

        XCTAssertThrowsError(
            try JSONDecoder().decode(
                MessagePage.self,
                from: JSONSerialization.data(withJSONObject: object)
            )
        )
    }

    func testMessageRejectsReceivedStatusOnOutboundDirection() throws {
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: ContentTestFixtures.data(named: "messages-page.json")
            ) as? [String: Any]
        )
        var items = try XCTUnwrap(object["items"] as? [[String: Any]])
        items[0]["status"] = "RECEIVED"
        object["items"] = items

        XCTAssertThrowsError(
            try JSONDecoder().decode(
                MessagePage.self,
                from: JSONSerialization.data(withJSONObject: object)
            )
        )
    }
}
