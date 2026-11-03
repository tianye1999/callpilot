package ai.bondings.callpilot.protocol

import java.io.IOException
import java.net.URI
import java.util.UUID
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import okhttp3.Call
import okhttp3.Callback
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
    private val client: OkHttpClient = HttpClients.shared,
    private val clockMs: () -> Long = System::currentTimeMillis,
    private val sleepMs: (Long) -> Unit = Thread::sleep,
    private val longPollClient: OkHttpClient = HttpClients.longPoll,
    private val quickClient: OkHttpClient = HttpClients.quick,
) : MessageContentClient, CallRecordContentClient {
    companion object {
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()
        private val DEVICE_ID = Regex("^device_[A-Za-z0-9_-]{12,80}$")
        private val EDGE_ID = Regex("^edge_[A-Za-z0-9_-]{12,80}$")
        private val CALL_ID = Regex("^call_[A-Za-z0-9_-]{12,80}$")
        private val OFFER_ID = Regex("^offer_[A-Za-z0-9_-]{12,80}$")
        private val CLAIM_ID = Regex("^claim_[A-Za-z0-9_-]{12,80}$")
        private const val MAX_SDP_BYTES = 16 * 1024
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
            val features = payload["features"]?.jsonObject ?: JsonObject(emptyMap())
            return HostedDeviceStatus(paired, device, edge, features)
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
        val created = createCallWithRetry(body, edgeId).call
        throwIfTerminal(created)
        return pollCallSession(edgeId, created.callId, created.expiresAt)
    }

    /** 轮询到 Edge 签发 LiveKit 凭证（Edge 在 call 变 ready 后才可用）。 */
    private fun pollCallSession(edgeId: String, callId: String, expiresAt: Long): HostedCallSession {
        val pollingStartedAt = clockMs()
        while (clockMs() < expiresAt) {
            val call = request("GET", "v1/calls/$callId", null).use { response ->
                decodeCall(parseOrThrow(response), response.code).also {
                    validateCall(
                        it,
                        expectedCallId = callId,
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
            val remaining = expiresAt - now
            if (remaining <= 0) break
            val interval = if (now - pollingStartedAt < 3_000) 250L else 1_000L
            sleepMs(minOf(interval, remaining))
        }
        throw HostedCloudException(408, "SESSION_TIMEOUT", "等待云端通话会话超时")
    }

    /** A8：外呼直连用的 TURN/STUN 凭证；中转或 Edge 没开直连时中转回 503，调用方改走 LiveKit。 */
    fun outboundIceServers(edgeId: String): List<IceServer> {
        require(EDGE_ID.matches(edgeId)) { "云配对缺少有效的 Edge ID" }
        val body = buildJsonObject { put("edgeId", edgeId) }.toString()
        return request("POST", "v1/calls/ice", body).use { response -> parseIceServers(parseOrThrow(response), response.code) }
    }

    /**
     * A8：带完整 offer 建外呼，Edge 的 answer 随响应返回。没有 answer 时看 directAttempted：
     * true 表示 Edge 已为这通开了会话、只能走同一通的 LiveKit 回落；false 表示中转没走直连（旧 Edge），照旧轮询。
     */
    fun createDirectCall(
        edgeId: String,
        sdpOffer: String,
        idempotencyKey: String = "android-${UUID.randomUUID()}",
    ): OutboundCall {
        require(EDGE_ID.matches(edgeId)) { "云配对缺少有效的 Edge ID" }
        require(IDEMPOTENCY_KEY.matches(idempotencyKey)) { "idempotency key 格式不合法" }
        require(sdpOffer.startsWith("v=0") && sdpOffer.toByteArray().size <= MAX_SDP_BYTES) { "sdp offer 不合法" }
        val body = buildJsonObject {
            put("edgeId", edgeId)
            put("idempotencyKey", idempotencyKey)
            put("mediaTransport", "webrtc")
            put("sdpOffer", sdpOffer)
        }.toString()
        val created = createCallWithRetry(body, edgeId)
        val call = created.call
        val payload = created.payload
        val code = created.statusCode
        if (call.status == "failed" || call.status == "ended") {
            val errorCode = payload["errorCode"]?.jsonPrimitive?.contentOrNull ?: "CALL_FAILED"
            throw HostedCloudException(code, errorCode, "云端呼叫创建失败")
        }
        val directAttempted = payload.strictBoolean("directAttempted", code) ?: false
        val webrtc = payload["transports"]?.jsonArray?.map { it.jsonObject }
            ?.firstOrNull { it["kind"]?.jsonPrimitive?.contentOrNull == "webrtc" }
        val sdpAnswer = (webrtc?.get("sdpAnswer") as? JsonPrimitive)?.takeIf { it.isString }?.content
        val contradictory = (!directAttempted && webrtc != null) || (webrtc != null && sdpAnswer == null) ||
            (sdpAnswer != null && (!sdpAnswer.startsWith("v=0") || sdpAnswer.toByteArray().size > MAX_SDP_BYTES)) ||
            (directAttempted && call.session != null)
        if (contradictory) throw HostedCloudException(code, "INVALID_RESPONSE", "直连外呼响应不一致")
        if (directAttempted && sdpAnswer == null && call.status == "pending") {
            // 同 key 重放时首次建呼叫可能还在等 Edge：等它定下来（failed 直接报错）。定下来后以最终状态为准——
            // Edge 取不到 TURN 时中转会改成普通 LiveKit 会话，那时 directAttempted 为 false，走轮询而不是回落。
            val settled = awaitCallSettled(edgeId, call.callId, call.expiresAt)
            return OutboundCall(call.callId, call.expiresAt, settled, null)
        }
        return OutboundCall(call.callId, call.expiresAt, directAttempted, sdpAnswer)
    }

    /** 轮询到离开 pending，返回最终是否仍是直连通话。 */
    private fun awaitCallSettled(edgeId: String, callId: String, expiresAt: Long): Boolean {
        val startedAt = clockMs()
        while (clockMs() < expiresAt) {
            val (call, direct) = request("GET", "v1/calls/$callId", null).use { response ->
                val payload = parseOrThrow(response)
                val decoded = decodeCall(payload, response.code).also {
                    validateCall(it, expectedCallId = callId, expectedEdgeId = edgeId, statusCode = response.code)
                }
                decoded to (payload.strictBoolean("directAttempted", response.code) ?: false)
            }
            throwIfTerminal(call)
            if (call.status != "pending") return direct
            val now = clockMs()
            sleepMs(minOf(if (now - startedAt < 3_000) 250L else 1_000L, maxOf(0L, expiresAt - now)))
        }
        throw HostedCloudException(408, "SESSION_TIMEOUT", "等待云端通话会话超时")
    }

    /** A8：中转没走直连（未开启或旧 Edge）时沿用轮询拿 LiveKit 凭证。 */
    fun awaitCallSession(edgeId: String, call: OutboundCall): HostedCallSession {
        require(EDGE_ID.matches(edgeId) && CALL_ID.matches(call.callId)) { "标识格式不合法" }
        return pollCallSession(edgeId, call.callId, call.expiresAt)
    }

    /** A8：直连没连上，同一通切到 LiveKit 房间（Edge 端幂等，绝不重新建呼叫）。 */
    fun fallbackCallLiveKit(callId: String): HostedCallSession {
        require(CALL_ID.matches(callId)) { "call id 格式不合法" }
        return request("POST", "v1/calls/$callId/media/livekit", "{}").use { response ->
            val payload = parseOrThrow(response)
            val session = payload["session"] as? JsonObject
            val url = (session?.get("livekitUrl") as? JsonPrimitive)?.takeIf { it.isString }?.content
            val token = (session?.get("token") as? JsonPrimitive)?.takeIf { it.isString }?.content
            val expiresAt = (session?.get("expiresAt") as? JsonPrimitive)?.longOrNull
            if (payload["callId"]?.jsonPrimitive?.contentOrNull != callId || url == null || token == null || expiresAt == null) {
                throw HostedCloudException(response.code, "INVALID_RESPONSE", "外呼回落响应不匹配")
            }
            validateSession(HostedSessionPayload(url, token, expiresAt))
            HostedCallSession(sessionId = callId, livekitUrl = url, token = token, expiresAt = expiresAt)
        }
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

    /**
     * #95：claim 一个 offer，成功即拿到入房凭证（first-claim-wins，输家收 409）。
     * `direct=true` 时请求 webrtc 直连（可带已准备好的 sdpOffer）；中转未开直连则按旧响应回落。
     */
    fun claimInboundOffer(offerId: String, sdpOffer: String? = null, direct: Boolean = false): HostedCallSession {
        require(OFFER_ID.matches(offerId)) { "offer id 格式不合法" }
        require(sdpOffer == null || (sdpOffer.startsWith("v=0") && sdpOffer.toByteArray().size <= MAX_SDP_BYTES)) { "sdp offer 不合法" }
        val body = buildJsonObject {
            put("offerId", offerId)
            if (direct) put("mediaTransport", "webrtc")
            if (direct && sdpOffer != null) put("sdpOffer", sdpOffer)
        }.toString()
        return request("POST", "v1/inbound-offers/claim", body).use { response ->
            decodeInboundSession(parseOrThrow(response), offerId, response.code, directRequested = direct, sdpSent = direct && sdpOffer != null)
        }
    }

    /** 直连用 TURN/STUN 凭证（响铃期或已 claim 后都可取）。 */
    fun inboundIceServers(offerId: String): List<IceServer> {
        require(OFFER_ID.matches(offerId)) { "offer id 格式不合法" }
        return request("POST", "v1/inbound-offers/$offerId/ice", "{}").use { response ->
            parseIceServers(parseOrThrow(response), response.code)
        }
    }

    private fun parseIceServers(payload: JsonObject, statusCode: Int): List<IceServer> {
        val servers = payload["iceServers"]?.jsonArray?.mapNotNull { element ->
            val obj = element.jsonObject
            val urls = obj["urls"]?.jsonArray?.mapNotNull { it.jsonPrimitive.contentOrNull } ?: emptyList()
            if (urls.isEmpty()) return@mapNotNull null
            IceServer(urls, obj["username"]?.jsonPrimitive?.contentOrNull, obj["credential"]?.jsonPrimitive?.contentOrNull)
        } ?: emptyList()
        if (servers.isEmpty()) throw HostedCloudException(statusCode, "INVALID_RESPONSE", "ICE 服务器列表为空")
        return servers
    }

    /** claim 时 SDP 还没准备好（sdpPending）：事后补交，拿 Edge 的 answer。 */
    fun completeInboundSdp(offerId: String, claimId: String, sdpOffer: String): HostedCallSession {
        require(OFFER_ID.matches(offerId) && CLAIM_ID.matches(claimId)) { "标识格式不合法" }
        require(sdpOffer.startsWith("v=0") && sdpOffer.toByteArray().size <= MAX_SDP_BYTES) { "sdp offer 不合法" }
        val body = buildJsonObject {
            put("claimId", claimId)
            put("sdpOffer", sdpOffer)
        }.toString()
        return request("POST", "v1/inbound-offers/$offerId/media/sdp", body).use { response ->
            val session = decodeInboundSession(parseOrThrow(response), offerId, response.code, directRequested = true, sdpSent = true)
            if (session.sessionId != claimId || session.sdpPending) {
                throw HostedCloudException(response.code, "INVALID_RESPONSE", "补交 SDP 响应不匹配")
            }
            session
        }
    }

    /** 直连失败：同一 claim 下切到 LiveKit 房间，绝不重新 claim。 */
    fun fallbackInboundLiveKit(offerId: String, claimId: String, clientTiming: List<String> = emptyList()): HostedCallSession {
        require(OFFER_ID.matches(offerId) && CLAIM_ID.matches(claimId)) { "标识格式不合法" }
        val body = mediaBody(claimId, clientTiming)
        return request("POST", "v1/inbound-offers/$offerId/media/livekit", body).use { response ->
            val session = decodeInboundSession(parseOrThrow(response), offerId, response.code, directRequested = true, sdpSent = false)
            if (session.sessionId != claimId) throw HostedCloudException(response.code, "INVALID_RESPONSE", "回落响应不匹配")
            session
        }
    }

    /** 机主挂断：告知中转该 claim 已结束（幂等）。 */
    fun endInboundClaim(offerId: String, claimId: String, clientTiming: List<String> = emptyList()) {
        require(OFFER_ID.matches(offerId) && CLAIM_ID.matches(claimId)) { "标识格式不合法" }
        val body = mediaBody(claimId, clientTiming)
        request("POST", "v1/inbound-offers/$offerId/media/end", body).use { response -> parseOrThrow(response) }
    }

    /** claim 围栏 + 可选的客户端里程碑（CallTiming.httpBatches 产出，只含固定名与毫秒）。 */
    private fun mediaBody(claimId: String, clientTiming: List<String>): String = buildJsonObject {
        put("claimId", claimId)
        if (clientTiming.isNotEmpty()) put("clientTiming", JsonArray(clientTiming.map { json.parseToJsonElement(it) }))
    }.toString()

    private fun JsonObject.strictBoolean(key: String, statusCode: Int): Boolean? {
        val value = this[key] ?: return null
        val primitive = value as? JsonPrimitive
        if (primitive == null || primitive.isString || primitive.booleanOrNull == null) {
            throw HostedCloudException(statusCode, "INVALID_RESPONSE", "$key 不是布尔值")
        }
        return primitive.booleanOrNull
    }

    private fun decodeInboundSession(
        payload: JsonObject,
        offerId: String,
        statusCode: Int,
        directRequested: Boolean,
        sdpSent: Boolean,
    ): HostedCallSession {
        val claimId = payload["claimId"]?.jsonPrimitive?.contentOrNull
        val echoedOfferId = payload["offerId"]?.jsonPrimitive?.contentOrNull
        val expiresAt = payload["expiresAt"]?.jsonPrimitive?.longOrNull
        val transports = payload["transports"]?.jsonArray?.map { it.jsonObject } ?: emptyList()
        val livekit = transports.firstOrNull { it["kind"]?.jsonPrimitive?.contentOrNull == "livekit" }
        val webrtc = transports.firstOrNull { it["kind"]?.jsonPrimitive?.contentOrNull == "webrtc" }
        val url = (livekit?.get("url") ?: payload["url"])?.jsonPrimitive?.contentOrNull
        val token = (livekit?.get("token") ?: payload["token"])?.jsonPrimitive?.contentOrNull
        if (claimId == null || url == null || token == null || expiresAt == null) {
            throw HostedCloudException(statusCode, "INVALID_RESPONSE", "接管响应字段不完整")
        }
        if (!CLAIM_ID.matches(claimId) || echoedOfferId != offerId) {
            throw HostedCloudException(statusCode, "INVALID_RESPONSE", "接管响应标识不匹配")
        }
        val session = HostedSessionPayload(livekitUrl = url, token = token, expiresAt = expiresAt)
        validateSession(session)
        val directAttempted = payload.strictBoolean("directAttempted", statusCode) ?: false
        val sdpPending = payload.strictBoolean("sdpPending", statusCode) ?: false
        val sdpAnswer = (webrtc?.get("sdpAnswer") as? JsonPrimitive)?.takeIf { it.isString }?.content
        val contradictory = (!directRequested && (directAttempted || sdpPending || webrtc != null)) ||
            (!directAttempted && (sdpPending || sdpAnswer != null)) ||
            (sdpAnswer != null && (!sdpSent || sdpPending || sdpAnswer.isBlank() || sdpAnswer.toByteArray().size > MAX_SDP_BYTES))
        if (contradictory) {
            // 没请求直连却给了直连字段、没发 offer 却来了 answer、answer 与 pending 自相矛盾：协议被破坏，宁可不信。
            throw HostedCloudException(statusCode, "INVALID_RESPONSE", "直连响应与请求不一致")
        }
        val iceServers = webrtc?.get("iceServers")?.jsonArray?.mapNotNull { element ->
            val obj = element.jsonObject
            val urls = obj["urls"]?.jsonArray?.mapNotNull { it.jsonPrimitive.contentOrNull } ?: emptyList()
            if (urls.isEmpty()) null else IceServer(urls, obj["username"]?.jsonPrimitive?.contentOrNull, obj["credential"]?.jsonPrimitive?.contentOrNull)
        } ?: emptyList()
        return HostedCallSession(
            sessionId = claimId,
            livekitUrl = session.livekitUrl,
            token = session.token,
            expiresAt = session.expiresAt,
            directAttempted = directAttempted,
            sdpPending = sdpPending,
            sdpAnswer = sdpAnswer,
            iceServers = iceServers,
        )
    }

    /**
     * 长轮询 offer 的机主侧终态：他机接听 / 机主拒绝 / 结束时立即返回，否则中转最多挂 20 s
     * 再回 offered。能力未开启时中转回 404 FEATURE_DISABLED，由调用方退回轮询。
     */
    fun watchInboundOffer(offerId: String): InboundOfferState {
        require(OFFER_ID.matches(offerId)) { "offer id 格式不合法" }
        return request("POST", "v1/inbound-offers/$offerId/watch", "{}", http = longPollClient).use { response ->
            offerState(parseOrThrow(response), response.code)
        }
    }

    /** 同上，但可随协程取消而取消底层请求（收铃、通话开始、切配对时不必等 20 s 长轮询）。 */
    suspend fun watchInboundOfferCancellable(offerId: String): InboundOfferState {
        require(OFFER_ID.matches(offerId)) { "offer id 格式不合法" }
        val call = longPollClient.newCall(buildRequest("POST", "v1/inbound-offers/$offerId/watch", "{}", emptyList()))
        val response = suspendCancellableCoroutine<Response> { continuation ->
            continuation.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    if (continuation.isActive) continuation.resumeWithException(e)
                }

                override fun onResponse(call: Call, response: Response) {
                    if (continuation.isActive) continuation.resume(response) else response.close()
                }
            })
        }
        return response.use { offerState(parseOrThrow(it), it.code) }
    }

    /**
     * 机主拒绝：中转立刻让 Edge 交给 AI 接听，本通不再转回手机。200 为已拒绝（幂等）；
     * 409 且带 state 表示已被接听 / 已结束，照常返回该状态而不是抛错。
     */
    fun declineInboundOffer(offerId: String): InboundOfferState {
        require(OFFER_ID.matches(offerId)) { "offer id 格式不合法" }
        return request("POST", "v1/inbound-offers/$offerId/decline", "{}", http = quickClient).use { response ->
            val bytes = response.body?.bytes() ?: ByteArray(0)
            if (response.code == 409) {
                val payload = runCatching { json.parseToJsonElement(bytes.toString(Charsets.UTF_8)).jsonObject }.getOrNull()
                InboundOfferState.parse(payload?.get("state")?.jsonPrimitive?.contentOrNull)?.let { return@use it }
            }
            offerState(parseOrThrow(response, bytes), response.code)
        }
    }

    private fun offerState(payload: JsonObject, statusCode: Int): InboundOfferState =
        InboundOfferState.parse(payload["state"]?.jsonPrimitive?.contentOrNull)
            ?: throw HostedCloudException(statusCode, "INVALID_RESPONSE", "来电状态响应不合法")

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
        http: OkHttpClient = client,
    ): Response = http.newCall(buildRequest(method, path, jsonBody, query)).execute()

    private fun buildRequest(
        method: String,
        path: String,
        jsonBody: String?,
        query: List<Pair<String, String>>,
    ): Request {
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
        return builder.build()
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

    private class CreatedCall(val call: HostedCallResponse, val payload: JsonObject, val statusCode: Int)

    private fun createCallWithRetry(body: String, edgeId: String): CreatedCall {
        repeat(2) { attempt ->
            try {
                return request("POST", "v1/calls", body).use { response ->
                    val payload = parseOrThrow(response)
                    val call = decodeCall(payload, response.code).also {
                        validateCall(it, expectedEdgeId = edgeId, statusCode = response.code)
                    }
                    CreatedCall(call, payload, response.code)
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
            val code = call.errorCode?.takeIf { Regex("^[A-Z][A-Z0-9_]{2,63}$").matches(it) } ?: "CALL_FAILED"
            throw HostedCloudException(200, code, "云端呼叫创建失败")
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
    val features: JsonObject = JsonObject(emptyMap()),
) {
    val connected: Boolean
        get() = edge["connected"]?.jsonPrimitive?.booleanOrNull ?: false
    val modemOnline: Boolean
        get() = edge["modemOnline"]?.jsonPrimitive?.booleanOrNull ?: false
    /** 中转只在真 JSON 布尔为 true 时才算开启；字符串 "true" 与缺失都算关闭（与 iOS 一致）。 */
    val multiDeviceStop: Boolean
        get() = features.strictTrue("multiDeviceStop")
    val ownerDecline: Boolean
        get() = features.strictTrue("ownerDecline")
    val inboundFastpathV2: Boolean
        get() = features.strictTrue("inboundFastpathV2")
    val outboundDirectV1: Boolean
        get() = features.strictTrue("outboundDirectV1")

    private fun JsonObject.strictTrue(key: String): Boolean =
        (this[key] as? JsonPrimitive)?.let { !it.isString && it.booleanOrNull == true } ?: false
}

/** 来电 offer 的机主侧状态（中转 watch / decline 的应答）。 */
enum class InboundOfferState(val wire: String) {
    OFFERED("offered"),
    CLAIMED("claimed"),
    ANSWERED_ELSEWHERE("answered_elsewhere"),
    OWNER_DECLINED("owner_declined"),
    ENDED("ended");

    companion object {
        fun parse(value: String?): InboundOfferState? = entries.firstOrNull { it.wire == value }
    }
}

data class HostedCallSession(
    val sessionId: String,
    val livekitUrl: String,
    val token: String,
    val expiresAt: Long,
    /** 中转走了直连流程（V2）；false 即旧 LiveKit 响应。 */
    val directAttempted: Boolean = false,
    /** claim 时没带 SDP，需用 completeInboundSdp 补交。 */
    val sdpPending: Boolean = false,
    /** Edge 的 answer（含其候选），有它就能直连。 */
    val sdpAnswer: String? = null,
    val iceServers: List<IceServer> = emptyList(),
) {
    override fun toString(): String =
        "HostedCallSession(sessionId=$sessionId, livekitUrl=$livekitUrl, token=***, expiresAt=$expiresAt, direct=$directAttempted, sdpPending=$sdpPending)"
}

/** A8：一通外呼的直连建立结果；sdpAnswer 为空时按 directAttempted 决定回落还是轮询。 */
data class OutboundCall(
    val callId: String,
    val expiresAt: Long,
    val directAttempted: Boolean,
    val sdpAnswer: String?,
) {
    override fun toString(): String = "OutboundCall(callId=$callId, direct=$directAttempted, answer=${sdpAnswer != null})"
}

/** TURN/STUN 服务器（凭证不入日志）。 */
data class IceServer(val urls: List<String>, val username: String? = null, val credential: String? = null) {
    override fun toString(): String = "IceServer(urls=$urls, credential=${if (credential != null) "***" else "none"})"
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
    val errorCode: String? = null,
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
