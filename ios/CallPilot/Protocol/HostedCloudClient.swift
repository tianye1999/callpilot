import Foundation
import CoreFoundation

/// Hosted `/v1` 适配器(对齐 Android `HostedCloudClient.kt`)。凭证与请求状态
/// 统一由 MainActor 隔离;仅依赖 Foundation/CoreFoundation。
@MainActor
final class HostedCloudClient: MessageContentClient, CallRecordContentClient {
    private let base: URL
    private let origin: String
    private let urlSession: URLSession
    private let clockMilliseconds: () -> Int64
    private let sleepMilliseconds: (UInt64) async throws -> Void
    var credential: DeviceCredential?

    private static let deviceCookieName = "__Host-callpilot-device"
    private static var deviceIdRE: Regex<Substring> { /^device_[A-Za-z0-9_-]{12,80}$/ }
    private static var edgeIdRE: Regex<Substring> { /^edge_[A-Za-z0-9_-]{12,80}$/ }
    private static var callIdRE: Regex<Substring> { /^call_[A-Za-z0-9_-]{12,80}$/ }
    private static var offerIdRE: Regex<Substring> { /^offer_[A-Za-z0-9_-]{12,80}$/ }
    private static var claimIdRE: Regex<Substring> { /^claim_[A-Za-z0-9_-]{12,80}$/ }
    private static var idempotencyKeyRE: Regex<Substring> { /^[A-Za-z0-9._:-]{16,128}$/ }

    init(
        baseURL: String,
        urlSession: URLSession = .shared,
        clockMilliseconds: @escaping () -> Int64 = {
            Int64(Date().timeIntervalSince1970 * 1_000)
        },
        sleepMilliseconds: @escaping (UInt64) async throws -> Void = { delay in
            try await Task.sleep(nanoseconds: delay * 1_000_000)
        }
    ) throws {
        guard let url = URL(string: baseURL), let scheme = url.scheme, let host = url.host else {
            throw HostedCloudError(statusCode: 0, code: "BAD_BASE_URL", message: "无效的网关地址")
        }
        self.base = url
        var originStr = "\(scheme)://\(host)"
        if let port = url.port { originStr += ":\(port)" }
        self.origin = originStr
        self.urlSession = urlSession
        self.clockMilliseconds = clockMilliseconds
        self.sleepMilliseconds = sleepMilliseconds
    }

    // MARK: - 配对

    func claimPairing(code: String, displayName: String) async throws -> HostedPairResult {
        let body = try JSONSerialization.data(
            withJSONObject: ["code": code, "displayName": displayName]
        )
        let (payload, response) = try await request("POST", "v1/pairing-sessions/claim", body: body)
        // Cookie 从 Set-Cookie 提取:__Host-callpilot-device=deviceId.secret
        guard let cred = Self.credentialFromSetCookie(response) else {
            throw HostedCloudError(statusCode: response.statusCode, code: "INVALID_RESPONSE",
                                   message: "配对响应缺少设备 Cookie")
        }
        guard let device = payload["device"] as? [String: Any],
              let deviceId = device["deviceId"] as? String,
              deviceId.wholeMatch(of: Self.deviceIdRE) != nil,
              deviceId == cred.deviceId,
              let edgeId = device["edgeId"] as? String,
              edgeId.wholeMatch(of: Self.edgeIdRE) != nil else {
            throw HostedCloudError(statusCode: response.statusCode, code: "INVALID_RESPONSE",
                                   message: "配对响应设备标识不合法")
        }
        credential = cred
        return HostedPairResult(deviceId: cred.deviceId, edgeId: edgeId, credential: cred)
    }

    // MARK: - 线路状态

    func deviceStatus() async throws -> HostedDeviceStatus {
        let (payload, _) = try await request("GET", "v1/device")
        let edge = payload["edge"] as? [String: Any]
        let connected = edge?["connected"] as? Bool ?? false
        let modemOnline = edge?["modemOnline"] as? Bool ?? false
        return HostedDeviceStatus(connected: connected, modemOnline: modemOnline)
    }

    // MARK: - PushKit token

    func registerVoipToken(_ token: String, environment: ApnsEnvironment) async throws {
        guard token.wholeMatch(of: /^[A-Fa-f0-9]{64}$/) != nil else {
            throw HostedCloudError(
                statusCode: 0,
                code: "BAD_PUSH_TOKEN",
                message: "VoIP push token is invalid"
            )
        }
        let body = try JSONSerialization.data(withJSONObject: [
            "token": token.lowercased(),
            "environment": environment.rawValue,
        ])
        _ = try await request("PUT", "v1/device/push-token", body: body)
    }

    func unregisterVoipToken() async throws {
        _ = try await request("DELETE", "v1/device/push-token")
    }

    // MARK: - 外呼会话

    /// 创建云端会话并轮询到 Edge 签发 LiveKit 凭证。号码不进入云 API，
    /// 必须等房间的 media_ready 事件后再经 data topic 发给 Edge。
    func createSession(
        edgeId: String,
        idempotencyKey: String = "ios-\(UUID().uuidString)"
    ) async throws -> HostedCallSession {
        guard edgeId.wholeMatch(of: Self.edgeIdRE) != nil else {
            throw HostedCloudError(statusCode: 0, code: "BAD_EDGE_ID", message: "云配对缺少有效的 Edge ID")
        }
        guard idempotencyKey.wholeMatch(of: Self.idempotencyKeyRE) != nil else {
            throw HostedCloudError(
                statusCode: 0,
                code: "BAD_IDEMPOTENCY_KEY",
                message: "idempotency key 格式不合法"
            )
        }

        let body = try JSONSerialization.data(withJSONObject: [
            "edgeId": edgeId,
            "idempotencyKey": idempotencyKey,
        ])
        let created = try await createCallWithRetry(body: body, expectedEdgeId: edgeId)
        try throwIfTerminal(created)

        let pollingStartedAt = clockMilliseconds()
        while clockMilliseconds() < created.expiresAt {
            let (payload, response) = try await request("GET", "v1/calls/\(created.callId)")
            let call = try decodeCall(payload, statusCode: response.statusCode)
            try validateCall(
                call,
                expectedCallId: created.callId,
                expectedEdgeId: edgeId,
                statusCode: response.statusCode
            )
            try throwIfTerminal(call)
            if let session = call.session {
                try validateSession(session, statusCode: response.statusCode)
                return HostedCallSession(
                    sessionId: call.callId,
                    livekitURL: session.livekitURL,
                    token: session.token,
                    expiresAt: session.expiresAt
                )
            }

            let now = clockMilliseconds()
            let remaining = created.expiresAt - now
            if remaining <= 0 { break }
            let interval: Int64 = now - pollingStartedAt < 3_000 ? 250 : 1_000
            try await sleepMilliseconds(UInt64(min(interval, remaining)))
        }
        throw HostedCloudError(statusCode: 408, code: "SESSION_TIMEOUT", message: "等待云端通话会话超时")
    }

    // MARK: - 来电接管(#95)

    /// 轮询本 Edge 当前可接管的 offer(仅 opaque id)。
    func listInboundOffers() async throws -> [InboundOffer] {
        let (payload, _) = try await request("GET", "v1/inbound-offers")
        guard let items = payload["offers"] as? [[String: Any]] else { return [] }
        return items.compactMap { item in
            guard let offerId = item["offerId"] as? String,
                  offerId.wholeMatch(of: Self.offerIdRE) != nil,
                  let expiresAt = Self.jsonInt64(item["expiresAt"]) else { return nil }
            let callUUID = (item["callUUID"] as? String).flatMap(UUID.init(uuidString:))
            return InboundOffer(offerId: offerId, callUUID: callUUID, expiresAt: expiresAt)
        }
    }

    /// claim 一个 offer,成功即拿到入房凭证(first-claim-wins,输家 409)。
    func claimInboundOffer(offerId: String) async throws -> HostedCallSession {
        guard offerId.wholeMatch(of: Self.offerIdRE) != nil else {
            throw HostedCloudError(statusCode: 0, code: "BAD_OFFER_ID", message: "offer id 格式不合法")
        }
        let body = try JSONSerialization.data(withJSONObject: ["offerId": offerId])
        let (payload, response) = try await request("POST", "v1/inbound-offers/claim", body: body)
        guard let claimId = payload["claimId"] as? String,
              claimId.wholeMatch(of: Self.claimIdRE) != nil,
              let echoed = payload["offerId"] as? String, echoed == offerId,
              let url = payload["url"] as? String,
              let token = payload["token"] as? String,
              let expiresAt = Self.jsonInt64(payload["expiresAt"]) else {
            throw HostedCloudError(statusCode: response.statusCode, code: "INVALID_RESPONSE",
                                   message: "接管响应字段不完整或标识不匹配")
        }
        let session = HostedSessionPayload(livekitURL: url, token: token, expiresAt: expiresAt)
        try validateSession(session, statusCode: response.statusCode)
        return HostedCallSession(
            sessionId: claimId,
            livekitURL: session.livekitURL,
            token: session.token,
            expiresAt: session.expiresAt
        )
    }

    // MARK: - 只读内容同步(#99)

    func listMessages(limit: Int = 25, cursor: String? = nil) async throws -> MessagePage {
        guard (1...100).contains(limit),
              ContentWireValidation.validCursor(cursor) else {
            throw HostedCloudError(
                statusCode: 0,
                code: "INVALID_REQUEST",
                message: "短信分页参数不合法"
            )
        }
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let cursor { queryItems.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await decodeContent(
            MessagePage.self,
            path: "v1/messages",
            queryItems: queryItems,
            responseName: "短信"
        )
    }

    func listCallRecords(limit: Int = 25, cursor: String? = nil) async throws -> CallRecordsPage {
        guard (1...100).contains(limit), ContentWireValidation.validCursor(cursor) else {
            throw HostedCloudError(
                statusCode: 0,
                code: "INVALID_REQUEST",
                message: "通话记录分页参数不合法"
            )
        }
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let cursor { queryItems.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await decodeContent(
            CallRecordsPage.self,
            path: "v1/call-records",
            queryItems: queryItems,
            responseName: "通话记录"
        )
    }

    func getCallRecord(callId: String) async throws -> CallRecordDetail {
        guard callId.wholeMatch(of: Self.callIdRE) != nil else {
            throw HostedCloudError(
                statusCode: 0,
                code: "INVALID_REQUEST",
                message: "通话记录标识不合法"
            )
        }
        return try await decodeContent(
            CallRecordDetail.self,
            path: "v1/call-records/\(callId)",
            queryItems: [],
            responseName: "通话详情"
        )
    }

    func listCallTimeline(
        callId: String,
        limit: Int = 50,
        cursor: String? = nil
    ) async throws -> CallTimelinePage {
        guard callId.wholeMatch(of: Self.callIdRE) != nil,
              (1...100).contains(limit),
              ContentWireValidation.validCursor(cursor) else {
            throw HostedCloudError(
                statusCode: 0,
                code: "INVALID_REQUEST",
                message: "通话时间线分页参数不合法"
            )
        }
        var queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let cursor { queryItems.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await decodeContent(
            CallTimelinePage.self,
            path: "v1/call-records/\(callId)/timeline",
            queryItems: queryItems,
            responseName: "通话时间线"
        )
    }

    // MARK: - 内部

    private func request(
        _ method: String, _ path: String, body: Data? = nil
    ) async throws -> ([String: Any], HTTPURLResponse) {
        var req = URLRequest(url: base.appendingPathComponent(path))
        req.httpMethod = method
        req.setValue(origin, forHTTPHeaderField: "Origin")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        if let cred = credential {
            req.setValue("\(Self.deviceCookieName)=\(cred.cookieValue)", forHTTPHeaderField: "Cookie")
        }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = body
        }
        let (data, http) = try await perform(req)
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        return (obj, http)
    }

    private func contentRequest(
        _ path: String,
        queryItems: [URLQueryItem]
    ) async throws -> (Data, HTTPURLResponse) {
        let endpoint = base.appendingPathComponent(path)
        guard var components = URLComponents(url: endpoint, resolvingAgainstBaseURL: false) else {
            throw HostedCloudError(statusCode: 0, code: "BAD_BASE_URL", message: "无效的网关地址")
        }
        if !queryItems.isEmpty { components.queryItems = queryItems }
        guard let url = components.url else {
            throw HostedCloudError(statusCode: 0, code: "INVALID_REQUEST", message: "分页参数不合法")
        }
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.setValue(origin, forHTTPHeaderField: "Origin")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        if let cred = credential {
            req.setValue("\(Self.deviceCookieName)=\(cred.cookieValue)", forHTTPHeaderField: "Cookie")
        }
        return try await perform(req)
    }

    private func decodeContent<T: Decodable>(
        _ type: T.Type,
        path: String,
        queryItems: [URLQueryItem],
        responseName: String
    ) async throws -> T {
        let (data, response) = try await contentRequest(path, queryItems: queryItems)
        guard data.count <= 16_384 else {
            throw HostedCloudError(
                statusCode: response.statusCode,
                code: "INVALID_RESPONSE",
                message: "\(responseName)响应超过协议上限"
            )
        }
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            throw HostedCloudError(
                statusCode: response.statusCode,
                code: "INVALID_RESPONSE",
                message: "\(responseName)响应不符合内容同步协议"
            )
        }
    }

    private func perform(_ req: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await urlSession.data(for: req)
        guard let http = response as? HTTPURLResponse else {
            throw HostedCloudError(statusCode: 0, code: "NO_HTTP_RESPONSE", message: "无 HTTP 响应")
        }
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        guard (200..<300).contains(http.statusCode) else {
            let err = obj["error"] as? [String: Any]
            throw HostedCloudError(
                statusCode: http.statusCode,
                code: (err?["code"] as? String) ?? "HTTP_\(http.statusCode)",
                message: (err?["message"] as? String) ?? "云控制面请求失败(HTTP \(http.statusCode))"
            )
        }
        return (data, http)
    }

    private struct HostedSessionPayload {
        let livekitURL: String
        let token: String
        let expiresAt: Int64
    }

    private struct HostedCallPayload {
        let callId: String
        let edgeId: String
        let status: String
        let createdAt: Int64
        let expiresAt: Int64
        let errorCode: String?
        let session: HostedSessionPayload?
    }

    private func createCallWithRetry(
        body: Data,
        expectedEdgeId: String
    ) async throws -> HostedCallPayload {
        for attempt in 0..<2 {
            do {
                let (payload, response) = try await request("POST", "v1/calls", body: body)
                let call = try decodeCall(payload, statusCode: response.statusCode)
                try validateCall(
                    call,
                    expectedCallId: nil,
                    expectedEdgeId: expectedEdgeId,
                    statusCode: response.statusCode
                )
                return call
            } catch is URLError where attempt == 0 {
                continue
            }
        }
        throw HostedCloudError(statusCode: 0, code: "TRANSPORT_ERROR", message: "云控制面请求失败")
    }

    private func decodeCall(
        _ payload: [String: Any],
        statusCode: Int
    ) throws -> HostedCallPayload {
        guard let callId = payload["callId"] as? String,
              let edgeId = payload["edgeId"] as? String,
              let status = payload["status"] as? String,
              let createdAt = Self.jsonInt64(payload["createdAt"]),
              let expiresAt = Self.jsonInt64(payload["expiresAt"]) else {
            throw HostedCloudError(
                statusCode: statusCode,
                code: "INVALID_RESPONSE",
                message: "云端呼叫响应字段不完整"
            )
        }

        var session: HostedSessionPayload?
        if let value = payload["session"] {
            guard status == "ready",
                  let fields = value as? [String: Any],
                  let livekitURL = fields["livekitUrl"] as? String,
                  let token = fields["token"] as? String,
                  let sessionExpiresAt = Self.jsonInt64(fields["expiresAt"]) else {
                throw HostedCloudError(
                    statusCode: statusCode,
                    code: "INVALID_RESPONSE",
                    message: "云端呼叫会话字段不完整"
                )
            }
            session = HostedSessionPayload(
                livekitURL: livekitURL,
                token: token,
                expiresAt: sessionExpiresAt
            )
        }

        return HostedCallPayload(
            callId: callId,
            edgeId: edgeId,
            status: status,
            createdAt: createdAt,
            expiresAt: expiresAt,
            errorCode: payload["errorCode"] as? String,
            session: session
        )
    }

    private func validateCall(
        _ call: HostedCallPayload,
        expectedCallId: String?,
        expectedEdgeId: String,
        statusCode: Int
    ) throws {
        let allowedStatuses = ["pending", "ready", "failed", "ended"]
        guard call.callId.wholeMatch(of: Self.callIdRE) != nil,
              call.edgeId == expectedEdgeId,
              expectedCallId == nil || call.callId == expectedCallId,
              allowedStatuses.contains(call.status),
              call.createdAt <= call.expiresAt,
              call.session == nil || call.status == "ready" else {
            throw HostedCloudError(
                statusCode: statusCode,
                code: "INVALID_RESPONSE",
                message: "云端呼叫响应内容不合法"
            )
        }
    }

    private func validateSession(
        _ session: HostedSessionPayload,
        statusCode: Int
    ) throws {
        guard let url = URL(string: session.livekitURL),
              url.scheme == "wss",
              url.host != nil,
              !session.token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              session.expiresAt > clockMilliseconds() else {
            throw HostedCloudError(
                statusCode: statusCode,
                code: "INVALID_RESPONSE",
                message: "云端会话连接信息不合法"
            )
        }
    }

    private func throwIfTerminal(_ call: HostedCallPayload) throws {
        guard call.status == "failed" || call.status == "ended" else { return }
        throw HostedCloudError(
            statusCode: 200,
            code: call.errorCode ?? "CALL_FAILED",
            message: "云端呼叫创建失败"
        )
    }

    private static func jsonInt64(_ value: Any?) -> Int64? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let integer = number.int64Value
        guard number.doubleValue.isFinite,
              number.doubleValue == Double(integer) else { return nil }
        return integer
    }

    private static func credentialFromSetCookie(_ response: HTTPURLResponse) -> DeviceCredential? {
        guard let raw = response.value(forHTTPHeaderField: "Set-Cookie") else { return nil }
        // 取 __Host-callpilot-device=deviceId.secret; 其余属性忽略
        for part in raw.split(separator: ";") {
            let kv = part.trimmingCharacters(in: .whitespaces)
            guard kv.hasPrefix("\(deviceCookieName)=") else { continue }
            let value = String(kv.dropFirst(deviceCookieName.count + 1))
            guard let dot = value.firstIndex(of: ".") else { return nil }
            let deviceId = String(value[value.startIndex..<dot])
            let secret = String(value[value.index(after: dot)...])
            guard !deviceId.isEmpty, !secret.isEmpty else { return nil }
            return DeviceCredential(deviceId: deviceId, secret: secret)
        }
        return nil
    }
}
