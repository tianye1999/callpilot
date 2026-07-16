import Foundation

enum MessageDirection: String, Codable, CaseIterable {
    case inbound = "INBOUND"
    case outbound = "OUTBOUND"
}

enum MessageDeliveryStatus: String, Codable, CaseIterable {
    case received = "RECEIVED"
    case sent = "SENT"
    case failed = "FAILED"
    case error = "ERROR"
}

struct SMSMessage: Codable, Equatable, Hashable, Identifiable {
    let messageId: String
    let revision: String
    let direction: MessageDirection
    let address: String
    let text: String
    let occurredAt: Int64
    let recordedAt: Int64
    let status: MessageDeliveryStatus

    var id: String { messageId }

    private static var messageIdRE: Regex<Substring> { /^msg_[A-Za-z0-9_-]{12,80}$/ }
    private static var revisionRE: Regex<Substring> { /^revision_[A-Za-z0-9_-]{12,80}$/ }

    init(
        messageId: String,
        revision: String,
        direction: MessageDirection,
        address: String,
        text: String,
        occurredAt: Int64,
        recordedAt: Int64,
        status: MessageDeliveryStatus
    ) {
        self.messageId = messageId
        self.revision = revision
        self.direction = direction
        self.address = address
        self.text = text
        self.occurredAt = occurredAt
        self.recordedAt = recordedAt
        self.status = status
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let messageId = try values.decode(String.self, forKey: .messageId)
        let revision = try values.decode(String.self, forKey: .revision)
        let direction = try values.decode(MessageDirection.self, forKey: .direction)
        let address = try values.decode(String.self, forKey: .address)
        let text = try values.decode(String.self, forKey: .text)
        let occurredAt = try values.decode(Int64.self, forKey: .occurredAt)
        let recordedAt = try values.decode(Int64.self, forKey: .recordedAt)
        let status = try values.decode(MessageDeliveryStatus.self, forKey: .status)

        guard messageId.wholeMatch(of: Self.messageIdRE) != nil,
              revision.wholeMatch(of: Self.revisionRE) != nil,
              occurredAt >= 0,
              recordedAt >= 0,
              status != .received || direction == .inbound else {
            throw DecodingError.dataCorruptedError(
                forKey: .messageId,
                in: values,
                debugDescription: "Invalid content-sync message"
            )
        }

        self.init(
            messageId: messageId,
            revision: revision,
            direction: direction,
            address: address,
            text: text,
            occurredAt: occurredAt,
            recordedAt: recordedAt,
            status: status
        )
    }
}

struct MessagePage: Codable, Equatable {
    let v: Int
    let items: [SMSMessage]
    let nextCursor: String?
    let hasMore: Bool
    let collectionRevision: String
    let oldestAvailableAt: Int64?

    private static var revisionRE: Regex<Substring> { /^revision_[A-Za-z0-9_-]{12,80}$/ }

    init(
        v: Int,
        items: [SMSMessage],
        nextCursor: String?,
        hasMore: Bool,
        collectionRevision: String,
        oldestAvailableAt: Int64?
    ) {
        self.v = v
        self.items = items
        self.nextCursor = nextCursor
        self.hasMore = hasMore
        self.collectionRevision = collectionRevision
        self.oldestAvailableAt = oldestAvailableAt
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let v = try values.decode(Int.self, forKey: .v)
        let items = try values.decode([SMSMessage].self, forKey: .items)
        let nextCursor = try values.decodeIfPresent(String.self, forKey: .nextCursor)
        let hasMore = try values.decode(Bool.self, forKey: .hasMore)
        let collectionRevision = try values.decode(String.self, forKey: .collectionRevision)
        let oldestAvailableAt = try values.decodeIfPresent(Int64.self, forKey: .oldestAvailableAt)

        guard v == 1,
              items.count <= 100,
              Set(items.map(\.messageId)).count == items.count,
              hasMore == (nextCursor != nil),
              ContentWireValidation.validCursor(nextCursor),
              collectionRevision.wholeMatch(of: Self.revisionRE) != nil,
              oldestAvailableAt == nil || oldestAvailableAt! >= 0 else {
            throw DecodingError.dataCorruptedError(
                forKey: .v,
                in: values,
                debugDescription: "Invalid content-sync message page"
            )
        }

        self.init(
            v: v,
            items: items,
            nextCursor: nextCursor,
            hasMore: hasMore,
            collectionRevision: collectionRevision,
            oldestAvailableAt: oldestAvailableAt
        )
    }
}

@MainActor
protocol MessageContentClient: AnyObject {
    func listMessages(limit: Int, cursor: String?) async throws -> MessagePage
}
