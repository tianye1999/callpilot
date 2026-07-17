package ai.bondings.callpilot.protocol

import java.io.IOException
import java.net.URI
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response

/** Hosted control-plane `/v1` adapter. Calls are blocking and must run on an IO dispatcher. */
class HostedCloudClient(
    baseUrl: String,
    private val client: OkHttpClient = OkHttpClient(),
    private val clockMs: () -> Long = System::currentTimeMillis,
    private val sleepMs: (Long) -> Unit = Thread::sleep,
) : MessageContentClient, CallRecordContentClient {
    companion object {
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()
        private val DEVICE_ID = Regex("^device_[A-Za-z0-9_-]{12,80}$")
        private val EDGE_ID = Regex("^edge_[A-Za-z0-9_-]{12,80}$")
        private val CALL_ID = Regex("^call_[A-Za-z0-9_-]{12,80}$")
        private val OFFER_ID = Regex("^offer_[A-Za-z0-9_-]{12,80}$")
        private val CLAIM_ID = Regex("^claim_[A-Za-z0-9_-]{12,80}$")
        private val IDEMPOTENCY_KEY = Regex("^[A-Za-z0-9._:-]{16,128}$")
        private const val CONTENT_RESPONSE_MAX_BYTES = 16_384
    }

    private val base: HttpUrl = baseUrl.toHttpUrl().also {
        require(it.isHttps || it.host in setOf("localhost", "127.0.0.1", "::1")) {
            "云控制面地址必须是 https"
        }
    }
    private val origin: String = buildString {
        append(base.scheme).append("://").append(base.host)
        if (base.port != HttpUrl.defaultPort(base.scheme)) append(":").append(base.port)
    }
    private val json = Json { ignoreUnknownKeys = true }

    @Volatile
    var credential: DeviceCredential? = null

    fun claimPairing(code: String, displayName: String): HostedPairResult {
        val body = buildJsonObject {
            put("code", code)
            put("displayName", displayName)
        }.toString()
        request("POST", "v1/pairing-sessions/claim", body).use { response ->
            val payload = parseOrThrow(response)
            val device = payload["device"]?.let {
                json.decodeFromJsonElement(HostedDevice.serializer(), it)
            } ?: throw HostedCloudException(response.code, "INVALID_RESPONSE", "配对响应缺少 device 字段")
            val deviceCredential = extractCredential(response)
                ?: throw HostedCloudException(response.code, "INVALID_RESPONSE", "配对响应缺少设备凭证 Cookie")
            validateDevice(device, deviceCredential, response.code)
            credential = deviceCredential
            return HostedPairResult(device, deviceCredential)
        }
    }

    fun deviceStatus(): HostedDeviceStatus {
        request("GET", "v1/device", null).use { response ->
            val payload = parseOrThrow(response)
            val paired = payload["paired"]?.jsonPrimitive?.booleanOrNull ?: false
            val device = payload["device"]?.let {
                json.decodeFromJsonElement(HostedDevice.serializer(), it)
            }
            if (paired) {
                validateDevice(
                    device ?: throw HostedCloudException(
                        response.code,
                        "INVALID_RESPONSE",
                        "设备状态响应缺少 device 字段",
                    ),
                    credential,
                    response.code,
                )
            }
            val edge = payload["edge"]?.jsonObject ?: JsonObject(emptyMap())
            return HostedDeviceStatus(paired, device, edge)
        }
    }

    fun createSession(
        edgeId: String,
        idempotencyKey: String = "android-${UUID.randomUUID()}",
    ): HostedCallSession {
        require(EDGE_ID.matches(edgeId)) { "云配对缺少有效的 Edge ID" }
        require(IDEMPOTENCY_KEY.matches(idempotencyKey)) { "idempotency key 格式不合法" }
        val body = buildJsonObject {
            put("edgeId", edgeId)
            put("idempotencyKey", idempotencyKey)
        }.toString()
        val created = createCallWithRetry(body, edgeId)
        throwIfTerminal(created)
        val pollingStartedAt = clockMs()
        while (clockMs() < created.expiresAt) {
            val call = request("GET", "v1/calls/${created.callId}", null).use { response ->
                decodeCall(parseOrThrow(response), response.code).also {
                    validateCall(
                        it,
                        expectedCallId = created.callId,
                        expectedEdgeId = edgeId,
                        statusCode = response.code,
                    )
                }
            }
            throwIfTerminal(call)
            call.session?.let {
                validateSession(it)
                return HostedCallSession(
                    sessionId = call.callId,
                    livekitUrl = it.livekitUrl,
                    token = it.token,
                    expiresAt = it.expiresAt,
                )
            }
            val now = clockMs()
            val remaining = created.expiresAt - now
            if (remaining <= 0) break
            val interval = if (now - pollingStartedAt < 3_000) 250L else 1_000L
            sleepMs(minOf(interval, remaining))
        }
        throw HostedCloudException(408, "SESSION_TIMEOUT", "等待云端通话会话超时")
    }

    /** #95 inbound takeover：轮询本 Edge 当前可接管的来电 offer（仅 opaque id）。 */
    fun listInboundOffers(): List<InboundOffer> {
        return request("GET", "v1/inbound-offers", null).use { response ->
            val payload = parseOrThrow(response)
            val items = payload["offers"]?.jsonArray ?: return emptyList()
            items.mapNotNull { element ->
                val obj = element.jsonObject
                val offerId = obj["offerId"]?.jsonPrimitive?.contentOrNull ?: return@mapNotNull null
                val expiresAt = obj["expiresAt"]?.jsonPrimitive?.longOrNull ?: return@mapNotNull null
                if (!OFFER_ID.matches(offerId)) return@mapNotNull null
                InboundOffer(offerId = offerId, expiresAt = expiresAt)
            }
        }
    }

    /** #95：claim 一个 offer，成功即拿到入房凭证（first-claim-wins，输家收 409）。 */
    fun claimInboundOffer(offerId: String): HostedCallSession {
        require(OFFER_ID.matches(offerId)) { "offer id 格式不合法" }
        val body = buildJsonObject { put("offerId", offerId) }.toString()
        return request("POST", "v1/inbound-offers/claim", body).use { response ->
            val payload = parseOrThrow(response)
            val claimId = payload["claimId"]?.jsonPrimitive?.contentOrNull
            val echoedOfferId = payload["offerId"]?.jsonPrimitive?.contentOrNull
            val url = payload["url"]?.jsonPrimitive?.contentOrNull
            val token = payload["token"]?.jsonPrimitive?.contentOrNull
            val expiresAt = payload["expiresAt"]?.jsonPrimitive?.longOrNull
            if (claimId == null || url == null || token == null || expiresAt == null) {
                throw HostedCloudException(response.code, "INVALID_RESPONSE", "接管响应字段不完整")
            }
            if (!CLAIM_ID.matches(claimId) || echoedOfferId != offerId) {
                throw HostedCloudException(response.code, "INVALID_RESPONSE", "接管响应标识不匹配")
            }
            val session = HostedSessionPayload(livekitUrl = url, token = token, expiresAt = expiresAt)
            validateSession(session)
            HostedCallSession(
                sessionId = claimId,
                livekitUrl = session.livekitUrl,
                token = session.token,
                expiresAt = session.expiresAt,
            )
        }
    }

    override suspend fun listMessages(limit: Int, cursor: String?): MessagePage {
        if (limit !in 1..100 || !ContentWireValidation.validCursor(cursor)) {
            throw HostedCloudException(0, "INVALID_REQUEST", "短信分页参数不合法")
        }
        return withContext(Dispatchers.IO) {
            val query = buildList {
                add("limit" to limit.toString())
                cursor?.let { add("cursor" to it) }
            }
            request("GET", "v1/messages", null, query).use { response ->
                decodeContent(response, "短信") { MessagePage.decode(json, it) }
            }
        }
    }

    override suspend fun listCallRecords(limit: Int, cursor: String?): CallRecordsPage {
        validateContentPage(limit, cursor, "通话记录")
        return withContext(Dispatchers.IO) {
            request("GET", "v1/call-records", null, pageQuery(limit, cursor)).use { response ->
                decodeContent(response, "通话记录") { CallRecordsPage.decode(json, it) }
            }
        }
    }

    override suspend fun getCallRecord(callId: String): CallRecordDetail {
        if (!ContentWireValidation.validCallId(callId)) {
            throw HostedCloudException(0, "INVALID_REQUEST", "通话记录标识不合法")
        }
        return withContext(Dispatchers.IO) {
            request("GET", "v1/call-records/$callId", null).use { response ->
                decodeContent(response, "通话详情") { CallRecordDetail.decode(json, it) }
            }
        }
    }

    override suspend fun listCallTimeline(
        callId: String,
        limit: Int,
        cursor: String?,
    ): CallTimelinePage {
        if (!ContentWireValidation.validCallId(callId)) {
            throw HostedCloudException(0, "INVALID_REQUEST", "通话记录标识不合法")
        }
        validateContentPage(limit, cursor, "通话时间线")
        return withContext(Dispatchers.IO) {
            request(
                "GET",
                "v1/call-records/$callId/timeline",
                null,
                pageQuery(limit, cursor),
            ).use { response ->
                decodeContent(response, "通话时间线") { CallTimelinePage.decode(json, it) }
            }
        }
    }

    fun unpair() {
        request("DELETE", "v1/device", null).use { response ->
            parseOrThrow(response)
            credential = null
        }
    }

    private fun request(
        method: String,
        path: String,
        jsonBody: String?,
        query: List<Pair<String, String>> = emptyList(),
    ): Response {
        val url = base.newBuilder().addPathSegments(path).apply {
            query.forEach { (name, value) -> addQueryParameter(name, value) }
        }.build()
        val builder = Request.Builder()
            .url(url)
            .header("Origin", origin)
            .header("Accept", "application/json")
            .header("Cache-Control", "no-store")
        credential?.let {
            builder.header("Cookie", "${GatewayClient.DEVICE_COOKIE}=${it.asCookieValue()}")
        }
        when (method) {
            "GET" -> builder.get()
            "POST" -> builder.post((jsonBody ?: "{}").toRequestBody(JSON_MEDIA))
            "DELETE" -> builder.delete()
            else -> error("不支持的方法 $method")
        }
        return client.newCall(builder.build()).execute()
    }

    private fun pageQuery(limit: Int, cursor: String?): List<Pair<String, String>> = buildList {
        add("limit" to limit.toString())
        cursor?.let { add("cursor" to it) }
    }

    private fun validateContentPage(limit: Int, cursor: String?, resourceName: String) {
        if (limit !in 1..100 || !ContentWireValidation.validCursor(cursor)) {
            throw HostedCloudException(0, "INVALID_REQUEST", "$resourceName 分页参数不合法")
        }
    }

    private fun <T> decodeContent(
        response: Response,
        responseName: String,
        decode: (String) -> T,
    ): T {
        val bytes = response.body?.bytes() ?: ByteArray(0)
        if (bytes.size > CONTENT_RESPONSE_MAX_BYTES) {
            throw HostedCloudException(
                response.code,
                "INVALID_RESPONSE",
                "$responseName 响应超过协议上限",
            )
        }
        val payload = parseOrThrow(response, bytes)
        return try {
            decode(payload.toString())
        } catch (_: ContentContractException) {
            throw HostedCloudException(
                response.code,
                "INVALID_RESPONSE",
                "$responseName 响应不符合内容同步协议",
            )
        }
    }

    private fun parseOrThrow(response: Response): JsonObject {
        return parseOrThrow(response, response.body?.bytes() ?: ByteArray(0))
    }

    private fun parseOrThrow(response: Response, bytes: ByteArray): JsonObject {
        val text = bytes.toString(Charsets.UTF_8)
        val payload = try {
            json.parseToJsonElement(text).jsonObject
        } catch (_: Exception) {
            throw HostedCloudException(
                response.code,
                "INVALID_RESPONSE",
                "云控制面响应不是合法 JSON（HTTP ${response.code}）",
            )
        }
        if (!response.isSuccessful) {
            val error = payload["error"]?.jsonObject
            val code = error?.get("code")?.jsonPrimitive?.contentOrNull ?: "HTTP_${response.code}"
            val message = error?.get("message")?.jsonPrimitive?.contentOrNull
                ?: "云控制面请求失败（HTTP ${response.code}）"
            throw HostedCloudException(response.code, code, message)
        }
        return payload
    }

    private fun decodeCall(payload: JsonObject, statusCode: Int): HostedCallResponse = try {
        json.decodeFromJsonElement(HostedCallResponse.serializer(), payload)
    } catch (_: Exception) {
        throw HostedCloudException(statusCode, "INVALID_RESPONSE", "云端呼叫响应字段不完整")
    }

    private fun createCallWithRetry(body: String, edgeId: String): HostedCallResponse {
        repeat(2) { attempt ->
            try {
                return request("POST", "v1/calls", body).use { response ->
                    decodeCall(parseOrThrow(response), response.code).also {
                        validateCall(it, expectedEdgeId = edgeId, statusCode = response.code)
                    }
                }
            } catch (e: IOException) {
                if (attempt == 1) throw e
            }
        }
        error("unreachable")
    }

    private fun validateCall(
        call: HostedCallResponse,
        expectedCallId: String? = null,
        expectedEdgeId: String,
        statusCode: Int,
    ) {
        val validCallId = CALL_ID.matches(call.callId)
        val matchesRequest = call.edgeId == expectedEdgeId &&
            (expectedCallId == null || call.callId == expectedCallId)
        val validSessionState = call.session == null || call.status == "ready"
        if (!validCallId || !matchesRequest || !validSessionState) {
            throw HostedCloudException(statusCode, "INVALID_RESPONSE", "云端呼叫响应内容不合法")
        }
    }

    private fun validateDevice(
        device: HostedDevice,
        expectedCredential: DeviceCredential?,
        statusCode: Int,
    ) {
        val matchesCredential = expectedCredential == null ||
            device.deviceId == expectedCredential.deviceId
        if (!DEVICE_ID.matches(device.deviceId) ||
            !EDGE_ID.matches(device.edgeId) ||
            !matchesCredential
        ) {
            throw HostedCloudException(statusCode, "INVALID_RESPONSE", "云端设备响应标识不匹配")
        }
    }

    private fun throwIfTerminal(call: HostedCallResponse) {
        if (call.status == "failed" || call.status == "ended") {
            throw HostedCloudException(200, "CALL_FAILED", "云端呼叫创建失败")
        }
    }

    private fun validateSession(session: HostedSessionPayload) {
        val uri = try {
            URI(session.livekitUrl)
        } catch (_: Exception) {
            null
        }
        if (uri?.scheme != "wss" ||
            uri.host.isNullOrBlank() ||
            session.token.isBlank() ||
            session.expiresAt <= clockMs()
        ) {
            throw HostedCloudException(200, "INVALID_RESPONSE", "云端会话连接信息不合法")
        }
    }

    private fun extractCredential(response: Response): DeviceCredential? {
        val header = response.headers("Set-Cookie")
            .firstOrNull { it.startsWith("${GatewayClient.DEVICE_COOKIE}=") } ?: return null
        val value = header.substringAfter('=').substringBefore(';')
        val separator = value.indexOf('.')
        if (separator <= 0 || separator == value.lastIndex) return null
        return DeviceCredential(value.substring(0, separator), value.substring(separator + 1))
    }

}

@Serializable
data class HostedDevice(
    val deviceId: String,
    val edgeId: String,
    val displayName: String = "",
)

data class HostedPairResult(
    val device: HostedDevice,
    val credential: DeviceCredential,
) {
    override fun toString(): String =
        "HostedPairResult(device=$device, credential=***)"
}

data class HostedDeviceStatus(
    val paired: Boolean,
    val device: HostedDevice?,
    val edge: JsonObject,
) {
    val connected: Boolean
        get() = edge["connected"]?.jsonPrimitive?.booleanOrNull ?: false
    val modemOnline: Boolean
        get() = edge["modemOnline"]?.jsonPrimitive?.booleanOrNull ?: false
}

data class HostedCallSession(
    val sessionId: String,
    val livekitUrl: String,
    val token: String,
    val expiresAt: Long,
) {
    override fun toString(): String =
        "HostedCallSession(sessionId=$sessionId, livekitUrl=$livekitUrl, token=***, expiresAt=$expiresAt)"
}

/** #95：一条可接管的来电 offer；云端只暴露 opaque id 与过期时间。 */
data class InboundOffer(
    val offerId: String,
    val expiresAt: Long,
)

@Serializable
private data class HostedCallResponse(
    val callId: String,
    val edgeId: String,
    val status: String,
    val createdAt: Long,
    val expiresAt: Long,
    val session: HostedSessionPayload? = null,
)

@Serializable
private data class HostedSessionPayload(
    val livekitUrl: String,
    val token: String,
    val expiresAt: Long,
)

class HostedCloudException(
    val statusCode: Int,
    val errorCode: String,
    override val message: String,
) : Exception(message)
