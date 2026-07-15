import Foundation

/// Hosted `/v1` 适配器(对齐 Android `HostedCloudClient.kt`)。方法为 async,
/// 调用方负责并发上下文。仅依赖 Foundation。
final class HostedCloudClient {
    private let base: URL
    private let origin: String
    private let urlSession: URLSession
    private let clockMilliseconds: () -> Int64
    var credential: DeviceCredential?

    private static let deviceCookieName = "__Host-callpilot-device"
    private static let deviceIdRE = /^device_[A-Za-z0-9_-]{12,80}$/
    private static let edgeIdRE = /^edge_[A-Za-z0-9_-]{12,80}$/
    private static let offerIdRE = /^offer_[A-Za-z0-9_-]{12,80}$/
    private static let claimIdRE = /^claim_[A-Za-z0-9_-]{12,80}$/

    init(
        baseURL: String,
        urlSession: URLSession = .shared,
        clockMilliseconds: @escaping () -> Int64 = {
            Int64(Date().timeIntervalSince1970 * 1_000)
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

    // MARK: - 来电接管(#95)

    /// 轮询本 Edge 当前可接管的 offer(仅 opaque id)。
    func listInboundOffers() async throws -> [InboundOffer] {
        let (payload, _) = try await request("GET", "v1/inbound-offers")
        guard let items = payload["offers"] as? [[String: Any]] else { return [] }
        return items.compactMap { item in
            guard let offerId = item["offerId"] as? String,
                  offerId.wholeMatch(of: Self.offerIdRE) != nil,
                  let expiresAt = (item["expiresAt"] as? NSNumber)?.int64Value else { return nil }
            return InboundOffer(offerId: offerId, expiresAt: expiresAt)
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
              let expiresAt = (payload["expiresAt"] as? NSNumber)?.int64Value else {
            throw HostedCloudError(statusCode: response.statusCode, code: "INVALID_RESPONSE",
                                   message: "接管响应字段不完整或标识不匹配")
        }
        guard let livekitURL = URL(string: url),
              livekitURL.scheme == "wss",
              livekitURL.host != nil,
              !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              expiresAt > clockMilliseconds() else {
            throw HostedCloudError(statusCode: response.statusCode, code: "INVALID_RESPONSE",
                                   message: "接管响应会话连接信息不合法")
        }
        return HostedCallSession(sessionId: claimId, livekitURL: url, token: token, expiresAt: expiresAt)
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
        return (obj, http)
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
