import Foundation

enum CallDirection: String, Codable, CaseIterable {
    case inbound = "INBOUND"
    case outbound = "OUTBOUND"
}

struct CallRecordStatus: RawRepresentable, Codable, Equatable, Hashable {
    let rawValue: String

    static let completed = Self(rawValue: "COMPLETED")
    static let notConnected = Self(rawValue: "NOT_CONNECTED")
    static let failed = Self(rawValue: "FAILED")
    static let unknown = Self(rawValue: "UNKNOWN")

    init(rawValue: String) { self.rawValue = rawValue }

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        guard value.wholeMatch(of: /^[A-Z][A-Z0-9_]{2,63}$/) != nil else {
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "Invalid product call status")
            )
        }
        rawValue = value
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

enum CallSource: String, Codable, CaseIterable {
    case agent = "AGENT"
    case remoteHandset = "REMOTE_HANDSET"
    case unknown = "UNKNOWN"
}

enum CallSummaryState: String, Codable, CaseIterable {
    case pending = "PENDING"
    case ready = "READY"
    case failed = "FAILED"
    case unavailable = "UNAVAILABLE"
}

enum CallTriageOutcome: String, Codable, CaseIterable {
    case aiHandled = "AI_HANDLED"
    case rejected = "REJECTED"
    case transferred = "TRANSFERRED"
    case unknown = "UNKNOWN"
}

struct CallRecordItem: Codable, Equatable, Hashable, Identifiable {
    let callId: String
    let revision: String
    let direction: CallDirection
    let address: String?
    let startedAt: Int64
    let endedAt: Int64?
    let durationMs: Int64?
    let status: CallRecordStatus
    let answered: Bool
    let source: CallSource
    let summaryState: CallSummaryState
    let summaryPreview: String?
    let hasTranscript: Bool
    let triageOutcome: CallTriageOutcome?

    var id: String { callId }

    private static var callIdRE: Regex<Substring> { /^call_[A-Za-z0-9_-]{12,80}$/ }
    private static var revisionRE: Regex<Substring> { /^revision_[A-Za-z0-9_-]{12,80}$/ }

    init(
        callId: String,
        revision: String,
        direction: CallDirection,
        address: String?,
        startedAt: Int64,
        endedAt: Int64?,
        durationMs: Int64?,
        status: CallRecordStatus,
        answered: Bool,
        source: CallSource,
        summaryState: CallSummaryState,
        summaryPreview: String?,
        hasTranscript: Bool,
        triageOutcome: CallTriageOutcome?
    ) {
        self.callId = callId
        self.revision = revision
        self.direction = direction
        self.address = address
        self.startedAt = startedAt
        self.endedAt = endedAt
        self.durationMs = durationMs
        self.status = status
        self.answered = answered
        self.source = source
        self.summaryState = summaryState
        self.summaryPreview = summaryPreview
        self.hasTranscript = hasTranscript
        self.triageOutcome = triageOutcome
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let callId = try values.decode(String.self, forKey: .callId)
        let revision = try values.decode(String.self, forKey: .revision)
        let direction = try values.decode(CallDirection.self, forKey: .direction)
        let address = try values.decodeIfPresent(String.self, forKey: .address)
        let startedAt = try values.decode(Int64.self, forKey: .startedAt)
        let endedAt = try values.decodeIfPresent(Int64.self, forKey: .endedAt)
        let durationMs = try values.decodeIfPresent(Int64.self, forKey: .durationMs)
        let status = try values.decode(CallRecordStatus.self, forKey: .status)
        let answered = try values.decode(Bool.self, forKey: .answered)
        let source = try values.decode(CallSource.self, forKey: .source)
        let summaryState = try values.decode(CallSummaryState.self, forKey: .summaryState)
        let summaryPreview = try values.decodeIfPresent(String.self, forKey: .summaryPreview)
        let hasTranscript = try values.decode(Bool.self, forKey: .hasTranscript)
        let triageOutcome = try values.decodeIfPresent(CallTriageOutcome.self, forKey: .triageOutcome)

        guard callId.wholeMatch(of: Self.callIdRE) != nil,
              revision.wholeMatch(of: Self.revisionRE) != nil,
              startedAt >= 0,
              endedAt == nil || endedAt! >= startedAt,
              durationMs == nil || durationMs! >= 0,
              (endedAt == nil) == (durationMs == nil) else {
            throw DecodingError.dataCorruptedError(
                forKey: .callId,
                in: values,
                debugDescription: "Invalid content-sync call record"
            )
        }

        self.init(
            callId: callId, revision: revision, direction: direction, address: address,
            startedAt: startedAt, endedAt: endedAt, durationMs: durationMs, status: status,
            answered: answered, source: source, summaryState: summaryState,
            summaryPreview: summaryPreview, hasTranscript: hasTranscript,
            triageOutcome: triageOutcome
        )
    }
}

struct CallRecordsPage: Codable, Equatable {
    let v: Int
    let items: [CallRecordItem]
    let nextCursor: String?
    let hasMore: Bool
    let collectionRevision: String
    let oldestAvailableAt: Int64?

    init(
        v: Int,
        items: [CallRecordItem],
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
        let items = try values.decode([CallRecordItem].self, forKey: .items)
        let nextCursor = try values.decodeIfPresent(String.self, forKey: .nextCursor)
        let hasMore = try values.decode(Bool.self, forKey: .hasMore)
        let revision = try values.decode(String.self, forKey: .collectionRevision)
        let oldest = try values.decodeIfPresent(Int64.self, forKey: .oldestAvailableAt)
        guard Self.validPage(
            v: v, ids: items.map(\.callId), nextCursor: nextCursor,
            hasMore: hasMore, revision: revision, oldest: oldest, itemCount: items.count
        ) else {
            throw DecodingError.dataCorruptedError(
                forKey: .v, in: values, debugDescription: "Invalid call records page"
            )
        }
        self.init(
            v: v, items: items, nextCursor: nextCursor, hasMore: hasMore,
            collectionRevision: revision, oldestAvailableAt: oldest
        )
    }

    private static func validPage(
        v: Int, ids: [String], nextCursor: String?, hasMore: Bool,
        revision: String, oldest: Int64?, itemCount: Int
    ) -> Bool {
        v == 1 && itemCount <= 100 && Set(ids).count == ids.count
            && hasMore == (nextCursor != nil)
            && ContentWireValidation.validCursor(nextCursor)
            && ContentWireValidation.validRevision(revision)
            && (oldest == nil || oldest! >= 0)
    }
}

struct CallSummary: Codable, Equatable, Hashable {
    let ok: Bool
    let text: String?
    let callerIdentity: String?
    let intent: String?
    let urgency: String?
    let callbackNeeded: Bool?
    let errorCode: String?
    let resultSource: String?
    let resultVerification: String?
}

struct CallRecordDetail: Codable, Equatable, Hashable {
    let v: Int
    let record: CallRecordItem
    let summary: CallSummary?
    let timelineRevision: String

    init(v: Int, record: CallRecordItem, summary: CallSummary?, timelineRevision: String) {
        self.v = v
        self.record = record
        self.summary = summary
        self.timelineRevision = timelineRevision
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let v = try values.decode(Int.self, forKey: .v)
        let record = try values.decode(CallRecordItem.self, forKey: .record)
        let summary = try values.decodeIfPresent(CallSummary.self, forKey: .summary)
        let timelineRevision = try values.decode(String.self, forKey: .timelineRevision)
        let mustBeNil = record.summaryState == .pending || record.summaryState == .unavailable
        guard v == 1,
              mustBeNil == (summary == nil),
              ContentWireValidation.validRevision(timelineRevision) else {
            throw DecodingError.dataCorruptedError(
                forKey: .summary, in: values, debugDescription: "Invalid call record detail"
            )
        }
        self.init(v: v, record: record, summary: summary, timelineRevision: timelineRevision)
    }
}

enum CallTimelineKind: String, Codable {
    case transcript
    case result
    case triage
    case takeover
    case unknown
}

enum TimelineRole: String, Codable { case agent = "AGENT"; case caller = "CALLER" }
enum TriageCategory: String, Codable { case marketing = "MARKETING"; case personal = "PERSONAL"; case needsOwner = "NEEDS_OWNER"; case unknown = "UNKNOWN" }
enum TriageAction: String, Codable { case clarify = "CLARIFY"; case continueAI = "CONTINUE_AI"; case reject = "REJECT"; case transfer = "TRANSFER" }
enum TakeoverState: String, Codable { case requested = "REQUESTED"; case committed = "COMMITTED"; case ownerHangup = "OWNER_HANGUP"; case failed = "FAILED" }

struct TimelineTranscript: Codable, Equatable, Hashable {
    let timelineItemId: String
    let occurredAt: Int64
    let type: String
    let role: TimelineRole
    let text: String
}

struct TimelineResult: Codable, Equatable, Hashable {
    let timelineItemId: String
    let occurredAt: Int64
    let type: String
    let status: CallRecordStatus
    let summary: String?
}

struct TimelineTriage: Codable, Equatable, Hashable {
    let timelineItemId: String
    let occurredAt: Int64
    let type: String
    let category: TriageCategory
    let action: TriageAction
    let confidence: Double
    let reasonCode: String
}

struct TimelineTakeover: Codable, Equatable, Hashable {
    let timelineItemId: String
    let occurredAt: Int64
    let type: String
    let state: TakeoverState
    let reasonCode: String?
}

struct TimelineUnknown: Codable, Equatable, Hashable {
    let timelineItemId: String
    let occurredAt: Int64
    let type: String
}

enum CallTimelineItem: Codable, Equatable, Hashable, Identifiable {
    case transcript(TimelineTranscript)
    case result(TimelineResult)
    case triage(TimelineTriage)
    case takeover(TimelineTakeover)
    case unknown(TimelineUnknown)

    var id: String { base.timelineItemId }
    var occurredAt: Int64 { base.occurredAt }
    var kind: CallTimelineKind {
        switch self {
        case .transcript: .transcript
        case .result: .result
        case .triage: .triage
        case .takeover: .takeover
        case .unknown: .unknown
        }
    }

    private var base: TimelineUnknown {
        switch self {
        case .transcript(let value): .init(timelineItemId: value.timelineItemId, occurredAt: value.occurredAt, type: value.type)
        case .result(let value): .init(timelineItemId: value.timelineItemId, occurredAt: value.occurredAt, type: value.type)
        case .triage(let value): .init(timelineItemId: value.timelineItemId, occurredAt: value.occurredAt, type: value.type)
        case .takeover(let value): .init(timelineItemId: value.timelineItemId, occurredAt: value.occurredAt, type: value.type)
        case .unknown(let value): value
        }
    }

    init(from decoder: Decoder) throws {
        let base = try TimelineUnknown(from: decoder)
        guard ContentWireValidation.validTimelineItemId(base.timelineItemId),
              base.occurredAt >= 0 else {
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "Invalid timeline item")
            )
        }
        switch base.type {
        case "TRANSCRIPT": self = .transcript(try TimelineTranscript(from: decoder))
        case "RESULT": self = .result(try TimelineResult(from: decoder))
        case "TRIAGE":
            let item = try TimelineTriage(from: decoder)
            guard (0...1).contains(item.confidence) else {
                throw DecodingError.dataCorrupted(
                    .init(codingPath: decoder.codingPath, debugDescription: "Invalid triage confidence")
                )
            }
            self = .triage(item)
        case "TAKEOVER": self = .takeover(try TimelineTakeover(from: decoder))
        default:
            guard base.type.wholeMatch(of: /^[A-Z][A-Z0-9_]{2,63}$/) != nil else {
                throw DecodingError.dataCorrupted(
                    .init(codingPath: decoder.codingPath, debugDescription: "Invalid future timeline type")
                )
            }
            self = .unknown(base)
        }
    }

    func encode(to encoder: Encoder) throws {
        switch self {
        case .transcript(let value): try value.encode(to: encoder)
        case .result(let value): try value.encode(to: encoder)
        case .triage(let value): try value.encode(to: encoder)
        case .takeover(let value): try value.encode(to: encoder)
        case .unknown(let value): try value.encode(to: encoder)
        }
    }
}

struct CallTimelinePage: Codable, Equatable {
    let v: Int
    let items: [CallTimelineItem]
    let nextCursor: String?
    let hasMore: Bool
    let collectionRevision: String
    let oldestAvailableAt: Int64?

    var visibleItems: [CallTimelineItem] { items.filter { $0.kind != .unknown } }

    init(
        v: Int,
        items: [CallTimelineItem],
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
        let items = try values.decode([CallTimelineItem].self, forKey: .items)
        let cursor = try values.decodeIfPresent(String.self, forKey: .nextCursor)
        let hasMore = try values.decode(Bool.self, forKey: .hasMore)
        let revision = try values.decode(String.self, forKey: .collectionRevision)
        let oldest = try values.decodeIfPresent(Int64.self, forKey: .oldestAvailableAt)
        guard v == 1, items.count <= 100,
              Set(items.map(\.id)).count == items.count,
              hasMore == (cursor != nil),
              ContentWireValidation.validCursor(cursor),
              ContentWireValidation.validRevision(revision),
              oldest == nil || oldest! >= 0 else {
            throw DecodingError.dataCorruptedError(
                forKey: .v, in: values, debugDescription: "Invalid call timeline page"
            )
        }
        self.init(
            v: v, items: items, nextCursor: cursor, hasMore: hasMore,
            collectionRevision: revision, oldestAvailableAt: oldest
        )
    }
}

enum ContentWireValidation {
    static func validCursor(_ value: String?) -> Bool {
        guard let value else { return true }
        return value.count <= 2_048
            && value.wholeMatch(of: /^cursor_[A-Za-z0-9_-]+$/) != nil
    }

    static func validRevision(_ value: String) -> Bool {
        value.wholeMatch(of: /^revision_[A-Za-z0-9_-]{12,80}$/) != nil
    }

    static func validTimelineItemId(_ value: String) -> Bool {
        value.wholeMatch(of: /^item_[A-Za-z0-9_-]{12,80}$/) != nil
    }
}

@MainActor
protocol CallRecordContentClient: AnyObject {
    func listCallRecords(limit: Int, cursor: String?) async throws -> CallRecordsPage
    func getCallRecord(callId: String) async throws -> CallRecordDetail
    func listCallTimeline(callId: String, limit: Int, cursor: String?) async throws -> CallTimelinePage
}
