import Foundation
import XCTest
@testable import CallPilot

@MainActor
final class CallMediaSessionTests: XCTestCase {
    func testStatusPayloadDrivesStateCallback() {
        // Android parity: CallManagerTest.`完整生命周期 拨号到本地挂断`.
        var states: [CallState] = []
        let session = CallMediaSession(onState: { states.append($0) })

        XCTAssertEqual(
            session.handleEdgePayload(Data(#"{"type":"status","status":"dialing"}"#.utf8), label: "10086"),
            .state(.dialing(number: "10086"))
        )
        XCTAssertEqual(states, [.dialing(number: "10086")])
    }

    func testConnectedPayloadRunsHandlerBeforeStateCallback() async {
        // Android parity: CallManagerTest.`answerTakeover connect 期间 connected 到达不被覆盖回等待态`.
        var callbacks: [String] = []
        let session = CallMediaSession(onState: { _ in callbacks.append("state") })
        session.installConnectedHandler { callbacks.append("connected") }

        _ = session.handleEdgePayload(
            Data(#"{"type":"status","status":"connected"}"#.utf8),
            label: "来电接管"
        )

        XCTAssertEqual(callbacks, ["connected", "state"])
    }

    func testMalformedPayloadDoesNotMutateState() {
        // Android parity: SignalingTest.`未知类型与坏 JSON 返回 null 而不是崩溃`.
        var states: [CallState] = []
        let session = CallMediaSession(onState: { states.append($0) })

        XCTAssertNil(session.handleEdgePayload(Data("not json".utf8), label: "10086"))
        XCTAssertTrue(states.isEmpty)
    }

    func testControlMethodsBuildPacketsWithoutLiveKitDependency() throws {
        // Android parity: SignalingTest.`dtmf 校验 0-9星井 1-16 位` and `hangup 命令`.
        let session = CallMediaSession(onState: { _ in })

        let dtmf = try XCTUnwrap(session.sendDTMF("2"))
        let dtmfFields = try XCTUnwrap(
            JSONSerialization.jsonObject(with: dtmf) as? [String: String]
        )
        let hangupFields = try XCTUnwrap(
            JSONSerialization.jsonObject(with: session.hangup()) as? [String: String]
        )

        XCTAssertEqual(dtmfFields, ["type": "dtmf", "digits": "2"])
        XCTAssertEqual(hangupFields, ["type": "hangup"])
        XCTAssertNil(session.sendDTMF("invalid"))
    }

    func testHangupFencesLateStatusFromSameSession() {
        // Android parity: CallManagerTest.`挂断与 media_ready 交错时绝不发送 dial`.
        var states: [CallState] = []
        let session = CallMediaSession(onState: { states.append($0) })

        _ = session.hangup()
        XCTAssertNil(session.handleEdgePayload(
            Data(#"{"type":"status","status":"dialing"}"#.utf8),
            label: "10086"
        ))

        XCTAssertEqual(states, [.ended(label: "通话", reason: "local_hangup")])
        XCTAssertNil(session.sendDTMF("2"))
    }

    func testStartOutboundPreparesSessionAndDefersDialPacket() async throws {
        // Android parity: CallManagerTest.`完整生命周期 拨号到本地挂断` waits for media_ready.
        var requestCount = 0
        MediaMockURLProtocol.requestHandler = { request in
            requestCount += 1
            let json: String
            let statusCode: Int
            if request.httpMethod == "POST" {
                statusCode = 202
                json = #"{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999}"#
            } else {
                statusCode = 200
                json = #"{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999,"session":{"livekitUrl":"wss://lk.example.com","token":"jwt-token","expiresAt":9999}}"#
            }
            return (
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: statusCode,
                    httpVersion: "HTTP/1.1",
                    headerFields: [:]
                )!,
                Data(json.utf8)
            )
        }
        defer { MediaMockURLProtocol.requestHandler = nil }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MediaMockURLProtocol.self]
        let client = try HostedCloudClient(
            baseURL: "https://cloud.example.test/",
            urlSession: URLSession(configuration: configuration),
            clockMilliseconds: { 1_000 },
            sleepMilliseconds: { _ in }
        )
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) })

        await media.startOutbound(
            client: client,
            edgeId: "edge_abcdefghijkl",
            number: "10086"
        )

        XCTAssertEqual(requestCount, 2)
        XCTAssertEqual(media.preparedSession?.sessionId, "call_abcdefghijkl")
        let dialPacket = try XCTUnwrap(media.takePendingDialPacket())
        let dial = try XCTUnwrap(
            JSONSerialization.jsonObject(with: dialPacket) as? [String: String]
        )
        XCTAssertEqual(dial["type"], "dial")
        XCTAssertEqual(dial["number"], "10086")
        XCTAssertNil(media.takePendingDialPacket())
        XCTAssertEqual(states, [.waitingMedia(label: "10086")])
    }
}

private final class MediaMockURLProtocol: URLProtocol {
    nonisolated(unsafe) static var requestHandler: (
        (URLRequest) throws -> (HTTPURLResponse, Data)
    )?

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.requestHandler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}
