package ai.bondings.callpilot.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

/**
 * LiveKit 房间内 data packet 信令（reliable）。
 * schema 与 Edge 端 remote_dialer.py / web 端 remote_dialer.js 对齐。
 */
object Topics {
    const val CONTROL = "callpilot.control"
    const val STATUS = "callpilot.status"
}

/** 与 Edge 端校验一致的本地预校验。 */
object Validation {
    private val NUMBER_RE = Regex("""\+?[0-9*#]{1,32}""")
    private val DTMF_RE = Regex("""[0-9*#]{1,16}""")

    fun isValidNumber(number: String): Boolean = NUMBER_RE.matches(number)
    fun isValidDtmf(digits: String): Boolean = DTMF_RE.matches(digits)
}

object Signaling {
    private val json = Json { ignoreUnknownKeys = true }

    fun encodeDial(number: String, idempotencyKey: String): String {
        require(Validation.isValidNumber(number)) { "号码格式不合法" }
        require(idempotencyKey.isNotBlank()) { "idempotency key 不能为空" }
        return buildJsonObject {
            put("type", "dial")
            put("number", number)
            put("idempotency_key", idempotencyKey)
        }.toString()
    }

    fun encodeHangup(): String = buildJsonObject { put("type", "hangup") }.toString()

    fun encodeDtmf(digits: String): String {
        require(Validation.isValidDtmf(digits)) { "DTMF 只允许 0-9*#，1-16 位" }
        return buildJsonObject {
            put("type", "dtmf")
            put("digits", digits)
        }.toString()
    }

    /** Edge → 客户端状态事件。未知类型返回 null（协议演进容错）。 */
    sealed interface Event {
        /** `{"type":"status","status":"...","reason":?,"code":?}` */
        data class Status(val status: String, val reason: String? = null) : Event

        /** `{"type":"remote_call","status":"dialing"|"connected"|...}` */
        data class RemoteCall(val status: String) : Event

        /** `{"type":"ended","reason":...}`：Edge 收尾时尽力发出的结束通知。 */
        data class Ended(val reason: String) : Event

        /** `{"type":"media_failed","reason":"no_phone_audio"}`：直连已通但 Edge 收不到手机音频，应回落 LiveKit。 */
        data class MediaFailed(val reason: String) : Event

        /** `{"type":"hangup.ack"}`：Edge 已收到挂断命令。 */
        data object HangupAck : Event
    }

    fun decodeEvent(raw: String): Event? {
        val obj = try {
            json.parseToJsonElement(raw).jsonObject
        } catch (_: Exception) {
            return null
        }
        // 字段类型错（如 "type":[]）也只当成未知事件，绝不让异常越过 native 回调。
        val type = (obj["type"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: return null
        val status = (obj["status"] as? JsonPrimitive)?.takeIf { it.isString }?.content
        val reason = ((obj["reason"] ?: obj["code"]) as? JsonPrimitive)?.takeIf { it.isString }?.content
        return when (type) {
            "status" -> status?.let { Event.Status(it, reason = reason) }
            "remote_call" -> status?.let { Event.RemoteCall(it) }
            "ended" -> Event.Ended(reason ?: "ended")
            "media_failed" -> Event.MediaFailed(reason ?: "media_failed")
            "hangup.ack" -> Event.HangupAck
            else -> null
        }
    }
}
