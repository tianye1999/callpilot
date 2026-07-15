import Foundation
import XCTest
@testable import CallPilot

final class HostedCloudClientTests: XCTestCase {
    override func tearDown() {
        MockURLProtocol.requestHandler = nil
        super.tearDown()
    }

    func testPairingUsesCamelCaseAndAcceptsMatchingCookie() async throws {
        // Android parity: HostedCloudClientTest.`claimPairing 使用 camelCase 并提取云凭证`.
        let client = try makeClient { request in
            XCTAssertEqual(request.url?.path, "/v1/pairing-sessions/claim")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Origin"), "https://cloud.example.test")
            let body = try XCTUnwrap(request.httpBody)
            let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: String])
            XCTAssertEqual(json["displayName"], "iPhone")
            return Self.response(
                for: request,
                status: 201,
                headers: [
                    "Set-Cookie": "__Host-callpilot-device=device_abcdefghijkl.secret-value; Path=/; Secure; HttpOnly"
                ],
                json: """
                {"paired":true,"device":{"deviceId":"device_abcdefghijkl","edgeId":"edge_abcdefghijkl","displayName":"iPhone"}}
                """
            )
        }

        let result = try await client.claimPairing(code: "ABCD-EFGH", displayName: "iPhone")

        XCTAssertEqual(result.edgeId, "edge_abcdefghijkl")
        XCTAssertEqual(result.credential, DeviceCredential(deviceId: "device_abcdefghijkl", secret: "secret-value"))
    }

    func testPairingRejectsCookieForAnotherDevice() async throws {
        // Android parity: HostedCloudClientTest.`claimPairing 拒绝与 device 不匹配的 Cookie 凭证`.
        let client = try makeClient { request in
            Self.response(
                for: request,
                status: 201,
                headers: [
                    "Set-Cookie": "__Host-callpilot-device=device_otherresponse.secret-value; Path=/; Secure"
                ],
                json: """
                {"paired":true,"device":{"deviceId":"device_abcdefghijkl","edgeId":"edge_abcdefghijkl","displayName":"iPhone"}}
                """
            )
        }

        do {
            _ = try await client.claimPairing(code: "ABCD-EFGH", displayName: "iPhone")
            XCTFail("Expected mismatched credentials to be rejected")
        } catch let error as HostedCloudError {
            XCTAssertEqual(error.code, "INVALID_RESPONSE")
            XCTAssertNil(client.credential)
        }
    }

    func testDeviceStatusReadsNestedEdgeAndSendsCredential() async throws {
        // Android parity: HostedCloudClientTest.`deviceStatus 与 unpair 都携带设备 Cookie`.
        let client = try makeClient { request in
            XCTAssertEqual(request.url?.path, "/v1/device")
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Cookie"),
                "__Host-callpilot-device=device_abcdefghijkl.secret-value"
            )
            return Self.response(
                for: request,
                json: """
                {"ok":true,"paired":true,"edge":{"connected":true,"modemOnline":true,"lineBusy":false}}
                """
            )
        }
        client.credential = DeviceCredential(deviceId: "device_abcdefghijkl", secret: "secret-value")

        let status = try await client.deviceStatus()

        XCTAssertEqual(status, HostedDeviceStatus(connected: true, modemOnline: true))
        XCTAssertTrue(status.lineReady)
    }

    func testListInboundOffersKeepsOnlyOpaqueValidItems() async throws {
        // Android parity: HostedCloudClientTest.`listInboundOffers 只解析 opaque offer 字段`.
        let client = try makeClient { request in
            XCTAssertEqual(request.url?.path, "/v1/inbound-offers")
            return Self.response(
                for: request,
                json: """
                {"offers":[
                  {"offerId":"offer_abcdefghijkl","expiresAt":9999999999999},
                  {"offerId":"not-an-offer","expiresAt":9999999999999},
                  {"offerId":"offer_missingexpiry"}
                ]}
                """
            )
        }

        let offers = try await client.listInboundOffers()

        XCTAssertEqual(offers, [InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9_999_999_999_999)])
    }

    func testClaimInboundOfferReturnsValidatedSession() async throws {
        // Android parity: HostedCloudClientTest.`claimInboundOffer 成功返回入房凭证`.
        let client = try makeClient(clockMilliseconds: { 1_000 }) { request in
            XCTAssertEqual(request.url?.path, "/v1/inbound-offers/claim")
            let body = try XCTUnwrap(request.httpBody)
            let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: String])
            XCTAssertEqual(json["offerId"], "offer_abcdefghijkl")
            return Self.response(
                for: request,
                status: 202,
                json: """
                {"claimId":"claim_abcdefghijkl","offerId":"offer_abcdefghijkl","url":"wss://lk.example.com","token":"a.b.c","expiresAt":9999}
                """
            )
        }

        let session = try await client.claimInboundOffer(offerId: "offer_abcdefghijkl")

        XCTAssertEqual(
            session,
            HostedCallSession(
                sessionId: "claim_abcdefghijkl",
                livekitURL: "wss://lk.example.com",
                token: "a.b.c",
                expiresAt: 9_999
            )
        )
    }

    func testClaimInboundOfferRejectsInvalidOrExpiredSession() async throws {
        // Android parity: HostedCloudClientTest.`ready 会话必须提供 wss 地址和非空 token`
        // and `ready 会话凭证已过期时拒绝 payload`.
        let client = try makeClient(clockMilliseconds: { 10_000 }) { request in
            Self.response(
                for: request,
                status: 202,
                json: """
                {"claimId":"claim_abcdefghijkl","offerId":"offer_abcdefghijkl","url":"https://lk.example.com","token":"","expiresAt":9999}
                """
            )
        }

        do {
            _ = try await client.claimInboundOffer(offerId: "offer_abcdefghijkl")
            XCTFail("Expected invalid media credentials to be rejected")
        } catch let error as HostedCloudError {
            XCTAssertEqual(error.code, "INVALID_RESPONSE")
        }
    }

    func testClaimInboundOfferPreservesStructuredError() async throws {
        // Android parity: HostedCloudClientTest.`claimInboundOffer 输家收到 409 抛结构化错误`.
        let client = try makeClient { request in
            Self.response(
                for: request,
                status: 409,
                json: """
                {"error":{"code":"OFFER_UNAVAILABLE","message":"already claimed"}}
                """
            )
        }

        do {
            _ = try await client.claimInboundOffer(offerId: "offer_abcdefghijkl")
            XCTFail("Expected first-claim-wins loser to fail")
        } catch let error as HostedCloudError {
            XCTAssertEqual(error.statusCode, 409)
            XCTAssertEqual(error.code, "OFFER_UNAVAILABLE")
            XCTAssertEqual(error.message, "already claimed")
        }
    }

    func testCreateSessionPostsThenPollsUntilReady() async throws {
        // Android parity: HostedCloudClientTest.`createSession 创建呼叫并轮询到 ready`.
        var requestCount = 0
        let client = try makeClient { request in
            requestCount += 1
            switch requestCount {
            case 1:
                XCTAssertEqual(request.httpMethod, "POST")
                XCTAssertEqual(request.url?.path, "/v1/calls")
                let body = try XCTUnwrap(request.httpBody)
                let fields = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: String])
                XCTAssertEqual(fields["edgeId"], "edge_abcdefghijkl")
                XCTAssertEqual(fields["idempotencyKey"], "ios-test-key-1234")
                return Self.response(
                    for: request,
                    status: 202,
                    json: Self.callJSON(status: "pending")
                )
            case 2:
                XCTAssertEqual(request.httpMethod, "GET")
                XCTAssertEqual(request.url?.path, "/v1/calls/call_abcdefghijkl")
                return Self.response(for: request, json: Self.callJSON(status: "pending"))
            default:
                XCTAssertEqual(request.url?.path, "/v1/calls/call_abcdefghijkl")
                return Self.response(
                    for: request,
                    json: Self.callJSON(
                        status: "ready",
                        session: #"{"livekitUrl":"wss://lk.example.com","token":"jwt-token","expiresAt":9999}"#
                    )
                )
            }
        }

        let session = try await client.createSession(
            edgeId: "edge_abcdefghijkl",
            idempotencyKey: "ios-test-key-1234"
        )

        XCTAssertEqual(requestCount, 3)
        XCTAssertEqual(session.sessionId, "call_abcdefghijkl")
        XCTAssertEqual(session.livekitURL, "wss://lk.example.com")
        XCTAssertEqual(session.token, "jwt-token")
    }

    func testCreateSessionPreservesStablePreflightError() async throws {
        // Android parity: HostedCloudClientTest.`结构化 API 错误按 code 暴露`.
        let client = try makeClient { request in
            Self.response(
                for: request,
                status: 409,
                json: #"{"error":{"code":"MODEM_OFFLINE","message":"Modem is offline"}}"#
            )
        }

        do {
            _ = try await client.createSession(
                edgeId: "edge_abcdefghijkl",
                idempotencyKey: "ios-test-key-1234"
            )
            XCTFail("Expected preflight rejection")
        } catch let error as HostedCloudError {
            XCTAssertEqual(error.statusCode, 409)
            XCTAssertEqual(error.code, "MODEM_OFFLINE")
            XCTAssertEqual(error.message, "Modem is offline")
        }
    }

    func testCreateSessionRejectsMismatchedPollIdentity() async throws {
        // Android parity: HostedCloudClientTest.`轮询响应的 callId 或 edgeId 不匹配时拒绝会话`.
        var requestCount = 0
        let client = try makeClient { request in
            requestCount += 1
            if requestCount == 1 {
                return Self.response(
                    for: request,
                    status: 202,
                    json: Self.callJSON(status: "pending")
                )
            }
            return Self.response(
                for: request,
                json: """
                {"callId":"call_otherresponse","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999,"session":{"livekitUrl":"wss://lk.example.com","token":"jwt-token","expiresAt":9999}}
                """
            )
        }

        do {
            _ = try await client.createSession(
                edgeId: "edge_abcdefghijkl",
                idempotencyKey: "ios-test-key-1234"
            )
            XCTFail("Expected mismatched call identity to fail closed")
        } catch let error as HostedCloudError {
            XCTAssertEqual(error.code, "INVALID_RESPONSE")
        }
    }

    func testCreateSessionRejectsInvalidReadyCredentials() async throws {
        // Android parity: HostedCloudClientTest.`ready 会话必须提供 wss 地址和非空 token`.
        var requestCount = 0
        let client = try makeClient { request in
            requestCount += 1
            if requestCount == 1 {
                return Self.response(
                    for: request,
                    status: 202,
                    json: Self.callJSON(status: "pending")
                )
            }
            return Self.response(
                for: request,
                json: Self.callJSON(
                    status: "ready",
                    session: #"{"livekitUrl":"https://lk.example.com","token":"","expiresAt":9999}"#
                )
            )
        }

        do {
            _ = try await client.createSession(
                edgeId: "edge_abcdefghijkl",
                idempotencyKey: "ios-test-key-1234"
            )
            XCTFail("Expected invalid room credentials to fail closed")
        } catch let error as HostedCloudError {
            XCTAssertEqual(error.code, "INVALID_RESPONSE")
        }
    }

    func testCreateSessionUsesPollErrorCodeForTerminalFailure() async throws {
        // Android parity: HostedCloudClientTest.`failed 呼叫停止轮询` with D1 errorCode preservation.
        var requestCount = 0
        let client = try makeClient { request in
            requestCount += 1
            let status = requestCount == 1 ? "pending" : "failed"
            let error = status == "failed" ? #", "errorCode":"SIM_NOT_READY""# : ""
            return Self.response(
                for: request,
                status: requestCount == 1 ? 202 : 200,
                json: """
                {"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"\(status)","createdAt":1,"expiresAt":9999\(error)}
                """
            )
        }

        do {
            _ = try await client.createSession(
                edgeId: "edge_abcdefghijkl",
                idempotencyKey: "ios-test-key-1234"
            )
            XCTFail("Expected terminal call failure")
        } catch let error as HostedCloudError {
            XCTAssertEqual(error.code, "SIM_NOT_READY")
            XCTAssertEqual(requestCount, 2)
        }
    }

    func testCreateSessionRetriesPostOnceWithIdenticalIdempotencyBody() async throws {
        // Android parity: HostedCloudClientTest.`POST 传输失败只用同一 idempotencyKey 重试一次`.
        var requestCount = 0
        var postBodies: [Data] = []
        let client = try makeClient { request in
            requestCount += 1
            if request.httpMethod == "POST" {
                postBodies.append(try XCTUnwrap(request.httpBody))
                if postBodies.count == 1 { throw URLError(.networkConnectionLost) }
                return Self.response(
                    for: request,
                    status: 202,
                    json: Self.callJSON(status: "pending")
                )
            }
            return Self.response(
                for: request,
                json: Self.callJSON(
                    status: "ready",
                    session: #"{"livekitUrl":"wss://lk.example.com","token":"jwt-token","expiresAt":9999}"#
                )
            )
        }

        let session = try await client.createSession(
            edgeId: "edge_abcdefghijkl",
            idempotencyKey: "ios-stable-key-123"
        )

        XCTAssertEqual(requestCount, 3)
        XCTAssertEqual(postBodies.count, 2)
        XCTAssertEqual(postBodies[0], postBodies[1])
        XCTAssertEqual(session.token, "jwt-token")
    }

    func testCreateSessionStopsAtServerDeadline() async throws {
        // Android parity: HostedCloudClientTest.`轮询到服务端 deadline 返回超时`.
        var requestCount = 0
        let client = try makeClient(clockMilliseconds: { 10_000 }) { request in
            requestCount += 1
            return Self.response(
                for: request,
                status: 202,
                json: Self.callJSON(status: "pending")
            )
        }

        do {
            _ = try await client.createSession(
                edgeId: "edge_abcdefghijkl",
                idempotencyKey: "ios-test-key-1234"
            )
            XCTFail("Expected deadline timeout")
        } catch let error as HostedCloudError {
            XCTAssertEqual(error.code, "SESSION_TIMEOUT")
            XCTAssertEqual(requestCount, 1)
        }
    }

    private func makeClient(
        clockMilliseconds: @escaping () -> Int64 = { 1_000 },
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> HostedCloudClient {
        MockURLProtocol.requestHandler = handler
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockURLProtocol.self]
        return try HostedCloudClient(
            baseURL: "https://cloud.example.test/",
            urlSession: URLSession(configuration: configuration),
            clockMilliseconds: clockMilliseconds,
            sleepMilliseconds: { _ in }
        )
    }

    private static func response(
        for request: URLRequest,
        status: Int = 200,
        headers: [String: String] = [:],
        json: String
    ) -> (HTTPURLResponse, Data) {
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: headers
        )!
        return (response, Data(json.utf8))
    }

    private static func callJSON(status: String, session: String? = nil) -> String {
        let sessionField = session.map { #", "session":\#($0)"# } ?? ""
        return """
        {"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"\(status)","createdAt":1,"expiresAt":9999\(sessionField)}
        """
    }
}

private final class MockURLProtocol: URLProtocol {
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
