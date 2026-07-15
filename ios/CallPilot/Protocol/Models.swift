import Foundation

// 平台无关协议模型。对齐 hosted `/v1` 契约(#42/#95)与 Android
// `protocol/Models.kt` + `HostedCloudClient.kt`。纯 Foundation,不依赖
// UIKit/SwiftUI/CallKit,命令行 `swiftc -typecheck` 即可验证。

/// 配对后持有的设备凭证(写入 __Host-callpilot-device Cookie)。
struct DeviceCredential: Equatable, Codable {
    let deviceId: String
    let secret: String

    /// Cookie 值:deviceId.secret(对齐 Android DeviceCredential.asCookieValue)。
    var cookieValue: String { "\(deviceId).\(secret)" }
}

/// 一次外呼或来电接管的入房凭证。
struct HostedCallSession: Equatable, CustomStringConvertible, CustomDebugStringConvertible {
    let sessionId: String
    let livekitURL: String
    let token: String
    let expiresAt: Int64

    /// Keep one-time room credentials out of logs and crash reports.
    var description: String {
        "HostedCallSession(sessionId: \(sessionId), livekitURL: \(livekitURL), token: ***, expiresAt: \(expiresAt))"
    }

    var debugDescription: String { description }
}

/// #95 一条可接管的来电 offer;云端只暴露 opaque id 与过期时间(无号码/转写)。
struct InboundOffer: Equatable {
    let offerId: String
    let expiresAt: Int64
}

/// 线路就绪状态(hosted `/api/device`)。
struct HostedDeviceStatus: Equatable {
    let connected: Bool
    let modemOnline: Bool

    /// 是否允许拨号/接管:电脑端在线且模组在线(对齐 Android connected && modemOnline)。
    var lineReady: Bool { connected && modemOnline }
}

/// 配对成功结果。
struct HostedPairResult: Equatable {
    let deviceId: String
    let edgeId: String
    let credential: DeviceCredential
}

/// 云控制面结构化错误(HTTP 非 2xx 时携带稳定 code)。
struct HostedCloudError: Error, Equatable {
    let statusCode: Int
    let code: String
    let message: String
}
