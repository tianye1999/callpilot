package ai.bondings.callpilot.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response

/**
 * 远程网关 HTTP 客户端（同步阻塞，调用方负责放到 IO 线程）。
 *
 * 原生端适配（见 docs/remote-protocol.md 第四节）：
 * - 手动携带 `__Host-callpilot-device` Cookie（网关只认 Cookie 鉴权）；
 * - 手动设置 `Origin` 头（`_same_origin` 校验要求与 public_origin 精确相等）。
 */
class GatewayClient(
    baseUrl: String,
    private val client: OkHttpClient = OkHttpClient(),
) {
    companion object {
        const val DEVICE_COOKIE = "__Host-callpilot-device"
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()
    }

    // 凭证以 __Host- Cookie 传输，明文网关会把它暴露在网络上；仅回环放行（本机调试/单测）
    private val base: HttpUrl = baseUrl.toHttpUrl().also {
        require(it.isHttps || it.host in setOf("localhost", "127.0.0.1", "::1")) {
            "网关地址必须是 https"
        }
    }
    private val origin: String = buildString {
        append(base.scheme).append("://").append(base.host)
        if (base.port != HttpUrl.defaultPort(base.scheme)) append(":").append(base.port)
    }
    private val json = Json { ignoreUnknownKeys = true }

    /** 当前设备凭证；配对成功后由 [pair] 填充，重启后由持久层注入。 */
    @Volatile
    var credential: DeviceCredential? = null

    data class PairResult(val device: PairedDevice, val credential: DeviceCredential)

    /** POST /api/pair —— 用一次性配对码换长期设备凭证（Set-Cookie 下发）。 */
    fun pair(code: String, displayName: String): PairResult {
        val body = buildJsonObject {
            put("code", code)
            put("display_name", displayName)
        }.toString()
        request("POST", "api/pair", body).use { resp ->
            val obj = parseOrThrow(resp)
            val cred = extractCredential(resp)
                ?: throw GatewayException(resp.code, "配对响应缺少设备凭证 Cookie")
            val device = obj["device"]?.let { json.decodeFromJsonElement(PairedDevice.serializer(), it) }
                ?: throw GatewayException(resp.code, "配对响应缺少 device 字段")
            credential = cred
            return PairResult(device, cred)
        }
    }

    /** GET /api/device —— 设备与 Edge 线路状态。 */
    fun deviceStatus(): DeviceStatus {
        request("GET", "api/device", null).use { resp ->
            val obj = parseOrThrow(resp)
            val paired = obj["paired"]?.jsonPrimitive?.booleanOrNull ?: false
            val device = obj["device"]?.let { json.decodeFromJsonElement(PairedDevice.serializer(), it) }
            val edge = obj["edge"]?.jsonObject ?: JsonObject(emptyMap())
            return DeviceStatus(paired = paired, device = device, edge = edge)
        }
    }

    /** POST /api/session —— 创建一次性拨号会话，返回邀请（fragment 内含 LiveKit 连接信息）。 */
    fun createSession(): Invite {
        request("POST", "api/session", "{}").use { resp ->
            val obj = parseOrThrow(resp)
            val invite = obj["invite"]
                ?: throw GatewayException(resp.code, "会话响应缺少 invite 字段")
            return json.decodeFromJsonElement(Invite.serializer(), invite)
        }
    }

    /** POST /api/unpair —— 撤销本设备配对。 */
    fun unpair() {
        request("POST", "api/unpair", "{}").use { resp ->
            parseOrThrow(resp)
            credential = null
        }
    }

    // ---- 内部 ----

    private fun request(method: String, path: String, jsonBody: String?): Response {
        val builder = Request.Builder()
            .url(base.newBuilder().addPathSegments(path).build())
            .header("Origin", origin)
            .header("Accept", "application/json")
        credential?.let { builder.header("Cookie", "$DEVICE_COOKIE=${it.asCookieValue()}") }
        when (method) {
            "GET" -> builder.get()
            "POST" -> builder.post((jsonBody ?: "{}").toRequestBody(JSON_MEDIA))
            else -> error("不支持的方法 $method")
        }
        return client.newCall(builder.build()).execute()
    }

    private fun parseOrThrow(resp: Response): JsonObject {
        val text = resp.body?.string().orEmpty()
        val obj = try {
            json.parseToJsonElement(text).jsonObject
        } catch (_: Exception) {
            throw GatewayException(resp.code, "网关响应不是合法 JSON（HTTP ${resp.code}）")
        }
        val ok = obj["ok"]?.jsonPrimitive?.booleanOrNull ?: false
        if (!resp.isSuccessful || !ok) {
            val message = obj["error"]?.jsonPrimitive?.contentOrNull
                ?: "网关请求失败（HTTP ${resp.code}）"
            throw GatewayException(resp.code, message)
        }
        return obj
    }

    /** 从 Set-Cookie 头提取 `<deviceId>.<secret>`。 */
    private fun extractCredential(resp: Response): DeviceCredential? {
        val header = resp.headers("Set-Cookie")
            .firstOrNull { it.startsWith("$DEVICE_COOKIE=") } ?: return null
        val value = header.removePrefix("$DEVICE_COOKIE=").substringBefore(';')
        val dot = value.indexOf('.')
        if (dot <= 0 || dot == value.length - 1) return null
        return DeviceCredential(value.substring(0, dot), value.substring(dot + 1))
    }
}
