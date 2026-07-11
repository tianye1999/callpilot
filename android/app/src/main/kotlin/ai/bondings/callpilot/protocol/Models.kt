package ai.bondings.callpilot.protocol

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonPrimitive

/**
 * 契约模型，字段与 docs/remote-protocol.md（基线 feat/issue-31-web-dialer@8003677）对齐。
 */

@Serializable
data class PairedDevice(
    @SerialName("device_id") val deviceId: String,
    @SerialName("display_name") val displayName: String = "",
    @SerialName("created_at") val createdAt: Double = 0.0,
    @SerialName("last_used_at") val lastUsedAt: Double = 0.0,
)

/**
 * `/api/session` 响应里的一次性拨号会话邀请。
 *
 * v0 契约（#37）：LiveKit 连接信息编码在 `url` 的 fragment 里；`token`/`livekitUrl`
 * 是服务端未来直发结构化字段的向前兼容位（两者都非空才生效，否则回退解 fragment）。
 */
@Serializable
data class Invite(
    @SerialName("session_id") val sessionId: String,
    val url: String,
    @SerialName("expires_at") val expiresAt: Double = 0.0,
    val token: String? = null,
    @SerialName("livekit_url") val livekitUrl: String? = null,
)

/** 邀请 URL fragment 解码后的 LiveKit 连接信息（`{"v":1,...}`）。 */
@Serializable
data class InvitePayload(
    val v: Int,
    val url: String,
    val token: String,
    val sessionId: String,
)

/** 配对凭证：以 `__Host-callpilot-device=<deviceId>.<secret>` Cookie 形式回传网关。 */
data class DeviceCredential(val deviceId: String, val secret: String) {
    fun asCookieValue(): String = "$deviceId.$secret"
}

/**
 * `/api/device` 响应。`edge` 字段清单待 #37 文档化，先以 JsonObject 透传，
 * 已知键用便捷属性暴露。
 */
data class DeviceStatus(
    val paired: Boolean,
    val device: PairedDevice?,
    val edge: JsonObject,
) {
    val edgeEnabled: Boolean
        get() = edge["enabled"]?.jsonPrimitive?.booleanOrNull ?: false
    val edgeConfigured: Boolean
        get() = edge["configured"]?.jsonPrimitive?.booleanOrNull ?: false
}

/** 网关返回 `{"ok":false,"error":"..."}` 时抛出。 */
class GatewayException(val statusCode: Int, override val message: String) : Exception(message)
