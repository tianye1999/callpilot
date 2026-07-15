import Foundation
import XCTest
@testable import CallPilot

final class CallSignalingTests: XCTestCase {
    func testDataTopicsMatchEdgeContract() {
        // Android parity: SignalingTest uses Topics.CONTROL and Topics.STATUS for LiveKit packets.
        XCTAssertEqual(CallPilotTopic.control, "callpilot.control")
        XCTAssertEqual(CallPilotTopic.status, "callpilot.status")
    }

    func testDtmfPacketMatchesEdgeSchema() throws {
        // Android parity: SignalingTest.`dtmf 校验 0-9星井 1-16 位`.
        let payload = try CallSignaling.encodeDTMF("1*#0")
        let fields = try jsonFields(payload)

        XCTAssertEqual(fields["type"] as? String, "dtmf")
        XCTAssertEqual(fields["digits"] as? String, "1*#0")
    }

    func testDtmfPacketRejectsInvalidDigits() {
        // Android parity: SignalingTest.`dtmf 校验 0-9星井 1-16 位`.
        XCTAssertThrowsError(try CallSignaling.encodeDTMF(""))
        XCTAssertThrowsError(try CallSignaling.encodeDTMF(String(repeating: "1", count: 17)))
        XCTAssertThrowsError(try CallSignaling.encodeDTMF("abc"))
    }

    func testHangupPacketMatchesEdgeSchema() throws {
        // Android parity: SignalingTest.`hangup 命令`.
        let fields = try jsonFields(CallSignaling.encodeHangup())

        XCTAssertEqual(fields["type"] as? String, "hangup")
        XCTAssertEqual(fields.count, 1)
    }

    func testDialPacketMatchesEdgeSchemaAndValidation() throws {
        // Android parity: SignalingTest.`dial 命令 schema 与 Edge 对齐`.
        let payload = try CallSignaling.encodeDial(
            number: "+8610020",
            idempotencyKey: "ios_12345678"
        )
        let fields = try jsonFields(payload)

        XCTAssertEqual(fields["type"] as? String, "dial")
        XCTAssertEqual(fields["number"] as? String, "+8610020")
        XCTAssertEqual(fields["idempotency_key"] as? String, "ios_12345678")
        XCTAssertThrowsError(try CallSignaling.encodeDial(number: "123abc", idempotencyKey: "ios_12345678"))
        XCTAssertThrowsError(try CallSignaling.encodeDial(number: "10086", idempotencyKey: "short"))
    }

    func testDecodeStatusAndRemoteCallEvents() {
        // Android parity: SignalingTest.`解析 status 与 remote_call 事件`.
        XCTAssertEqual(
            CallSignaling.decodeEvent(Data(#"{"type":"status","status":"media_ready"}"#.utf8)),
            .status(name: "media_ready", reason: nil, code: nil)
        )
        XCTAssertEqual(
            CallSignaling.decodeEvent(Data(#"{"type":"remote_call","status":"connected"}"#.utf8)),
            .remoteCall(status: "connected")
        )
    }

    func testDecodeStatusPreservesReasonAndCode() {
        // Android parity: SignalingTest.`status 事件解析 reason 或 code`.
        XCTAssertEqual(
            CallSignaling.decodeEvent(Data(#"{"type":"status","status":"failed","reason":"line busy","code":"LINE_BUSY"}"#.utf8)),
            .status(name: "failed", reason: "line busy", code: "LINE_BUSY")
        )
    }

    func testDecodeRejectsUnknownOrMalformedEvents() {
        // Android parity: SignalingTest.`未知类型与坏 JSON 返回 null 而不是崩溃`.
        XCTAssertNil(CallSignaling.decodeEvent(Data(#"{"type":"future","status":"x"}"#.utf8)))
        XCTAssertNil(CallSignaling.decodeEvent(Data(#"{"type":"status"}"#.utf8)))
        XCTAssertNil(CallSignaling.decodeEvent(Data("not json".utf8)))
        XCTAssertNil(CallSignaling.decodeEvent(Data("[]".utf8)))
    }

    private func jsonFields(_ data: Data) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }
}
