package ai.bondings.callpilot.protocol

import java.io.IOException
import kotlinx.coroutines.test.runTest
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class HostedCloudClientTest {
    @Test fun `outbound diagnostics preserve wire payload and reuse pooled connection`() {
        val body = """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999999999999,"directAttempted":false}"""
        server.enqueue(MockResponse().setBody(body))
        client.createDirectCall("edge_abcdefghijkl", "v=0\r\nsynthetic-sdp", "android-synthetic-key")
        val timing = ai.bondings.callpilot.call.OutboundTiming()
        server.enqueue(MockResponse().setBody(body).addHeader("Server-Timing", "edge_wait;dur=180;desc=\"secret-token\""))
        client.createDirectCall("edge_abcdefghijkl", "v=0\r\nsynthetic-sdp", "android-synthetic-key", timing)
        val first = server.takeRequest()
        val second = server.takeRequest()
        assertEquals(first.body.readUtf8(), second.body.readUtf8())
        assertEquals(1, second.sequenceNumber)
        val log = timing.line()
        assertFalse(log.contains("post_sent=-1"))
        assertFalse(log.contains("response_headers=-1"))
        assertFalse(log.contains("response=-1"))
        assertTrue(log.contains("tcp_start=-1"))
        assertTrue(log.contains("attempts=1"))
        assertTrue(log.endsWith("server_timing=edge_wait:180.0"))
        for (secret in listOf("secret-token", "synthetic-sdp", "android-synthetic-key", "edge_abcdefghijkl")) {
            assertFalse(log.contains(secret))
        }
    }

    private lateinit var server: MockWebServer
    private lateinit var client: HostedCloudClient
    private var nowMs = 1_000L
    private val sleeps = mutableListOf<Long>()

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        client = HostedCloudClient(
            baseUrl = server.url("/").toString(),
            clockMs = { nowMs },
            sleepMs = { delay ->
                sleeps += delay
                nowMs += delay
            },
        )
    }

    @After
    fun tearDown() = server.shutdown()

    private fun expectedOrigin(): String = "http://${server.hostName}:${server.port}"

    @Test
    fun `claimPairing 使用 camelCase 并提取云凭证`() {
        server.enqueue(
            MockResponse().setResponseCode(201)
                .addHeader(
                    "Set-Cookie",
                    "__Host-callpilot-device=device_abcdefghijkl.secret-value; Path=/; Secure; HttpOnly",
                )
                .setBody(
                    """{"paired":true,"device":{"deviceId":"device_abcdefghijkl","edgeId":"edge_abcdefghijkl","displayName":"Pixel"}}"""
                ),
        )

        val result = client.claimPairing("ABCD-EFGH", "Pixel")

        assertEquals("edge_abcdefghijkl", result.device.edgeId)
        assertEquals(DeviceCredential("device_abcdefghijkl", "secret-value"), result.credential)
        val request = server.takeRequest()
        assertEquals("/v1/pairing-sessions/claim", request.path)
        assertEquals(expectedOrigin(), request.getHeader("Origin"))
        assertTrue(request.body.readUtf8().contains("\"displayName\":\"Pixel\""))
    }

    @Test
    fun `claimPairing 拒绝与 device 不匹配的 Cookie 凭证`() {
        server.enqueue(
            MockResponse().setResponseCode(201)
                .addHeader(
                    "Set-Cookie",
                    "__Host-callpilot-device=device_otherresponse.secret-value; Path=/; Secure",
                )
                .setBody(
                    """{"paired":true,"device":{"deviceId":"device_abcdefghijkl","edgeId":"edge_abcdefghijkl","displayName":"Pixel"}}"""
                ),
        )

        val error = assertThrows(HostedCloudException::class.java) {
            client.claimPairing("ABCD-EFGH", "Pixel")
        }

        assertEquals("INVALID_RESPONSE", error.errorCode)
        assertEquals(null, client.credential)
    }

    @Test
    fun `createSession 创建呼叫并轮询到 ready`() {
        client.credential = DeviceCredential("device_abcdefghijkl", "secret-value")
        server.enqueue(
            MockResponse().setResponseCode(202).setBody(
                """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999}"""
            ),
        )
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999}"""
            ),
        )
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999,"session":{"livekitUrl":"wss://lk.example.com","token":"jwt-token","expiresAt":9999}}"""
            ),
        )

        val session = client.createSession("edge_abcdefghijkl", "android-1234567890")

        assertEquals("call_abcdefghijkl", session.sessionId)
        assertEquals("wss://lk.example.com", session.livekitUrl)
        assertEquals("jwt-token", session.token)
        val create = server.takeRequest()
        assertEquals("/v1/calls", create.path)
        assertEquals(
            "__Host-callpilot-device=device_abcdefghijkl.secret-value",
            create.getHeader("Cookie"),
        )
        val body = create.body.readUtf8()
        assertTrue(body.contains("\"edgeId\":\"edge_abcdefghijkl\""))
        assertTrue(body.contains("\"idempotencyKey\":\"android-1234567890\""))
        assertEquals("/v1/calls/call_abcdefghijkl", server.takeRequest().path)
        assertEquals("/v1/calls/call_abcdefghijkl", server.takeRequest().path)
    }

    @Test
    fun `failed 呼叫停止轮询`() {
        client.credential = DeviceCredential("device_abcdefghijkl", "secret-value")
        server.enqueue(
            MockResponse().setResponseCode(202).setBody(
                """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999}"""
            ),
        )
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"failed","createdAt":1,"expiresAt":9999}"""
            ),
        )
        val error = assertThrows(HostedCloudException::class.java) {
            client.createSession("edge_abcdefghijkl", "android-1234567890")
        }
        assertEquals("CALL_FAILED", error.errorCode)
    }

    @Test
    fun `创建响应已经 failed 时不再轮询`() {
        client.credential = DeviceCredential("device_abcdefghijkl", "secret-value")
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"failed","createdAt":1,"expiresAt":9999}"""
            ),
        )
        repeat(4) {
            server.enqueue(
                MockResponse().setResponseCode(200).setBody(
                    """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999}"""
                ),
            )
        }

        val error = assertThrows(HostedCloudException::class.java) {
            client.createSession("edge_abcdefghijkl", "android-1234567890")
        }

        assertEquals("CALL_FAILED", error.errorCode)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `轮询响应的 callId 或 edgeId 不匹配时拒绝会话`() {
        client.credential = DeviceCredential("device_abcdefghijkl", "secret-value")
        server.enqueue(
            MockResponse().setResponseCode(202).setBody(
                """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999}"""
            ),
        )
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"callId":"call_otherresponse","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999,"session":{"livekitUrl":"wss://lk.example.com","token":"jwt-token","expiresAt":9999}}"""
            ),
        )

        val error = assertThrows(HostedCloudException::class.java) {
            client.createSession("edge_abcdefghijkl", "android-1234567890")
        }

        assertEquals("INVALID_RESPONSE", error.errorCode)
    }

    @Test
    fun `ready 会话必须提供 wss 地址和非空 token`() {
        client.credential = DeviceCredential("device_abcdefghijkl", "secret-value")
        server.enqueue(
            MockResponse().setResponseCode(202).setBody(
                """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999}"""
            ),
        )
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999,"session":{"livekitUrl":"https://lk.example.com","token":"","expiresAt":9999}}"""
            ),
        )

        val error = assertThrows(HostedCloudException::class.java) {
            client.createSession("edge_abcdefghijkl", "android-1234567890")
        }

        assertEquals("INVALID_RESPONSE", error.errorCode)
    }

    @Test
    fun `ready 会话凭证已过期时拒绝 payload`() {
        client.credential = DeviceCredential("device_abcdefghijkl", "secret-value")
        server.enqueue(
            MockResponse().setResponseCode(202).setBody(
                """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999}"""
            ),
        )
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999,"session":{"livekitUrl":"wss://lk.example.com","token":"jwt-token","expiresAt":999}}"""
            ),
        )

        val error = assertThrows(HostedCloudException::class.java) {
            client.createSession("edge_abcdefghijkl", "android-1234567890")
        }

        assertEquals("INVALID_RESPONSE", error.errorCode)
    }

    @Test
    fun `结构化 API 错误按 code 暴露`() {
        server.enqueue(
            MockResponse().setResponseCode(409).setBody(
                """{"error":{"code":"EDGE_OFFLINE","message":"Edge is offline","requestId":"req_1"}}"""
            ),
        )

        val error = assertThrows(HostedCloudException::class.java) {
            client.createSession("edge_abcdefghijkl", "android-1234567890")
        }
        assertEquals(409, error.statusCode)
        assertEquals("EDGE_OFFLINE", error.errorCode)
        assertEquals("Edge is offline", error.message)
    }

    @Test
    fun `deviceStatus 与 unpair 都携带设备 Cookie`() {
        client.credential = DeviceCredential("device_abcdefghijkl", "secret-value")
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"ok":true,"paired":true,"device":{"deviceId":"device_abcdefghijkl","edgeId":"edge_abcdefghijkl","displayName":"Pixel"},"edge":{"connected":true,"modemOnline":true,"lineBusy":false}}"""
            ),
        )
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"paired":false}"""))

        val status = client.deviceStatus()
        assertTrue(status.connected)
        assertTrue(status.modemOnline)
        client.unpair()

        val get = server.takeRequest()
        assertEquals("GET", get.method)
        assertEquals("/v1/device", get.path)
        assertTrue(get.getHeader("Cookie")!!.startsWith("__Host-callpilot-device="))
        val delete = server.takeRequest()
        assertEquals("DELETE", delete.method)
        assertEquals(expectedOrigin(), delete.getHeader("Origin"))
        assertEquals(null, client.credential)
    }

    @Test
    fun `轮询到服务端 deadline 返回超时`() {
        nowMs = 1_000
        client = HostedCloudClient(
            baseUrl = server.url("/").toString(),
            clockMs = { nowMs },
            sleepMs = { nowMs += it },
        ).also {
            it.credential = DeviceCredential("device_abcdefghijkl", "secret-value")
        }
        repeat(3) { index ->
            server.enqueue(
                MockResponse().setResponseCode(if (index == 0) 202 else 200).setBody(
                    """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":1500}"""
                ),
            )
        }

        val error = assertThrows(HostedCloudException::class.java) {
            client.createSession("edge_abcdefghijkl", "android-1234567890")
        }
        assertEquals("SESSION_TIMEOUT", error.errorCode)
    }

    @Test
    fun `轮询以服务端毫秒 expiresAt 为 deadline 且三秒后降频`() {
        nowMs = 0
        sleeps.clear()
        client = HostedCloudClient(
            baseUrl = server.url("/").toString(),
            clockMs = { nowMs },
            sleepMs = { delay ->
                sleeps += delay
                nowMs += delay
            },
        ).also {
            it.credential = DeviceCredential("device_abcdefghijkl", "secret-value")
        }
        repeat(15) { index ->
            server.enqueue(
                MockResponse().setResponseCode(if (index == 0) 202 else 200).setBody(
                    """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":0,"expiresAt":5000}"""
                ),
            )
        }

        val error = assertThrows(HostedCloudException::class.java) {
            client.createSession("edge_abcdefghijkl", "android-1234567890")
        }

        assertEquals("SESSION_TIMEOUT", error.errorCode)
        assertEquals(List(12) { 250L } + listOf(1_000L, 1_000L), sleeps)
        assertEquals(5_000L, nowMs)
    }

    @Test
    fun `POST 传输失败只用同一 idempotencyKey 重试一次`() {
        // 全程 interceptor 合成响应、不真开 socket：MockWebServer + 关闭
        // retryOnConnectionFailure 的组合会禁用 OkHttp 路由回退，在 localhost
        // 双栈（::1/127.0.0.1）环境下产生与被测逻辑无关的 ConnectException。
        nowMs = 1_000
        val postBodies = mutableListOf<String>()
        val pendingBody =
            """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999}"""
        val readyBody =
            """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999,"session":{"livekitUrl":"wss://lk.example.com","token":"jwt-token","expiresAt":9999}}"""
        client = HostedCloudClient(
            baseUrl = "https://cloud.example.test/",
            client = OkHttpClient.Builder()
                .addInterceptor { chain ->
                    val request = chain.request()
                    val body = if (request.method == "POST") {
                        val buffer = Buffer()
                        request.body?.writeTo(buffer)
                        postBodies += buffer.readUtf8()
                        if (postBodies.size == 1) throw IOException("simulated transport failure")
                        pendingBody
                    } else {
                        readyBody
                    }
                    Response.Builder()
                        .request(request)
                        .protocol(Protocol.HTTP_1_1)
                        .code(if (request.method == "POST") 202 else 200)
                        .message("OK")
                        .body(body.toResponseBody("application/json".toMediaType()))
                        .build()
                }
                .build(),
            clockMs = { nowMs },
            sleepMs = { nowMs += it },
        ).also {
            it.credential = DeviceCredential("device_abcdefghijkl", "secret-value")
        }

        val result = client.createSession("edge_abcdefghijkl", "android-stable-key1")

        assertEquals(2, postBodies.size)
        assertEquals(postBodies[0], postBodies[1])
        assertTrue(postBodies[1].contains("android-stable-key1"))
        assertEquals("jwt-token", result.token)
    }
    @Test
    fun `listInboundOffers 只解析 opaque offer 字段`() {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setBody("""{"offers":[{"offerId":"offer_abcdefghijkl","expiresAt":9999999999999}]}"""),
        )

        val offers = client.listInboundOffers()

        assertEquals(1, offers.size)
        assertEquals("offer_abcdefghijkl", offers[0].offerId)
        assertEquals("/v1/inbound-offers", server.takeRequest().path)
    }

    @Test
    fun `claimInboundOffer 成功返回入房凭证`() {
        server.enqueue(
            MockResponse().setResponseCode(202)
                .setBody(
                    """{"claimId":"claim_abcdefghijkl","offerId":"offer_abcdefghijkl","roomName":"callpilot_x","url":"wss://cloud.livekit.example","token":"a.b.c","expiresAt":9999999999999}"""
                ),
        )

        val session = client.claimInboundOffer("offer_abcdefghijkl")

        assertEquals("claim_abcdefghijkl", session.sessionId)
        assertEquals("wss://cloud.livekit.example", session.livekitUrl)
        assertEquals("a.b.c", session.token)
        val request = server.takeRequest()
        assertEquals("/v1/inbound-offers/claim", request.path)
        assertTrue(request.body.readUtf8().contains("\"offerId\":\"offer_abcdefghijkl\""))
    }

    @Test
    fun `claimInboundOffer 输家收到 409 抛结构化错误`() {
        server.enqueue(
            MockResponse().setResponseCode(409)
                .setBody("""{"error":{"code":"OFFER_UNAVAILABLE","message":"already claimed"}}"""),
        )

        val error = assertThrows(HostedCloudException::class.java) {
            client.claimInboundOffer("offer_abcdefghijkl")
        }
        assertEquals("OFFER_UNAVAILABLE", error.errorCode)
    }

    @Test
    fun `watchInboundOffer 长轮询返回机主侧终态`() {
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"state":"answered_elsewhere"}"""))

        val state = client.watchInboundOffer("offer_abcdefghijkl")

        assertEquals(InboundOfferState.ANSWERED_ELSEWHERE, state)
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/v1/inbound-offers/offer_abcdefghijkl/watch", request.path)
    }

    @Test
    fun `watchInboundOffer 未知状态与能力关闭都不被当成 offered`() {
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"state":"something_new"}"""))
        server.enqueue(
            MockResponse().setResponseCode(404)
                .setBody("""{"error":{"code":"FEATURE_DISABLED","message":"Feature disabled"}}"""),
        )

        val invalid = assertThrows(HostedCloudException::class.java) { client.watchInboundOffer("offer_abcdefghijkl") }
        assertEquals("INVALID_RESPONSE", invalid.errorCode)
        val disabled = assertThrows(HostedCloudException::class.java) { client.watchInboundOffer("offer_abcdefghijkl") }
        assertEquals("FEATURE_DISABLED", disabled.errorCode)
        assertEquals(404, disabled.statusCode)
    }

    @Test
    fun `declineInboundOffer 成功幂等 409 带状态照常返回`() {
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"state":"owner_declined"}"""))
        server.enqueue(MockResponse().setResponseCode(409).setBody("""{"state":"claimed"}"""))
        server.enqueue(
            MockResponse().setResponseCode(409).setBody("""{"error":{"code":"FEATURE_DISABLED","message":"off"}}"""),
        )

        assertEquals(InboundOfferState.OWNER_DECLINED, client.declineInboundOffer("offer_abcdefghijkl"))
        assertEquals("/v1/inbound-offers/offer_abcdefghijkl/decline", server.takeRequest().path)
        assertEquals(InboundOfferState.CLAIMED, client.declineInboundOffer("offer_abcdefghijkl"))
        val error = assertThrows(HostedCloudException::class.java) { client.declineInboundOffer("offer_abcdefghijkl") }
        assertEquals("FEATURE_DISABLED", error.errorCode)
    }

    @Test
    fun `direct capability lookup fails closed without failing the call`() = runTest {
        val pairing = ai.bondings.callpilot.pairing.StoredPairing(
            server.url("/").toString(), "test", DeviceCredential("device_abcdefghijkl", "secret"),
            PairingProtocol.HOSTED, "edge_abcdefghijkl",
        )
        for (body in listOf("not-json", "{\"features\":[]}", "{}")) {
            server.enqueue(MockResponse().setBody(body))
            val status = ai.bondings.callpilot.call.DirectCallCapabilities.load(pairing)
            assertFalse(status?.outboundDirectV1 == true)
            assertFalse(status?.inboundFastpathV2 == true)
        }
        server.enqueue(MockResponse().setResponseCode(503).setBody("{}"))
        assertEquals(null, ai.bondings.callpilot.call.DirectCallCapabilities.load(pairing))
        assertEquals(null, ai.bondings.callpilot.call.DirectCallCapabilities.load(pairing.copy(protocol = PairingProtocol.TUNNEL)))
    }

    @Test
    fun `direct capabilities require true JSON booleans`() {
        for (value in listOf("true", "false", "null", "\"true\"", "{}", "[]")) {
            server.enqueue(MockResponse().setBody("""{"features":{"outboundDirectV1":$value,"inboundFastpathV2":$value}}"""))
            val status = client.deviceStatus()
            assertEquals(value == "true", status.outboundDirectV1)
            assertEquals(value == "true", status.inboundFastpathV2)
        }
        server.enqueue(MockResponse().setBody("""{}"""))
        val missing = client.deviceStatus()
        assertFalse(missing.outboundDirectV1)
        assertFalse(missing.inboundFastpathV2)
    }

    @Test
    fun `deviceStatus 只认真布尔的 features`() {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setBody("""{"paired":false,"edge":{"connected":true},"features":{"multiDeviceStop":true,"ownerDecline":"true","inboundFastpathV2":false}}"""),
        )

        val status = client.deviceStatus()

        assertTrue(status.multiDeviceStop)
        assertFalse(status.ownerDecline)
        assertFalse(status.inboundFastpathV2)
    }

    private val v2Claim = """{"claimId":"claim_abcdefghijkl","offerId":"offer_abcdefghijkl","callId":"call_abcdefghijkl","roomName":"r","url":"wss://cloud.livekit.example","token":"a.b.c","participantIdentity":"p","edgeIdentity":"e","expiresAt":9999999999999,"directAttempted":true,"sdpPending":true,"transports":[{"kind":"livekit","url":"wss://cloud.livekit.example","token":"a.b.c"}]}"""

    @Test
    fun `claimInboundOffer direct 请求 webrtc 并解析 V2 响应`() {
        server.enqueue(MockResponse().setResponseCode(202).setBody(v2Claim))

        val session = client.claimInboundOffer("offer_abcdefghijkl", direct = true)

        assertTrue(session.directAttempted)
        assertTrue(session.sdpPending)
        assertEquals(null, session.sdpAnswer)
        assertEquals("wss://cloud.livekit.example", session.livekitUrl)
        val body = server.takeRequest().body.readUtf8()
        assertTrue(body.contains("\"mediaTransport\":\"webrtc\""))
        assertFalse(body.contains("sdpOffer"))
    }

    @Test
    fun `claimInboundOffer 旧响应没有 directAttempted 即走 LiveKit`() {
        server.enqueue(
            MockResponse().setResponseCode(202)
                .setBody("""{"claimId":"claim_abcdefghijkl","offerId":"offer_abcdefghijkl","roomName":"r","url":"wss://cloud.livekit.example","token":"a.b.c","expiresAt":9999999999999}"""),
        )

        val session = client.claimInboundOffer("offer_abcdefghijkl", direct = true)

        assertFalse(session.directAttempted)
        assertFalse(session.sdpPending)
    }

    @Test
    fun `completeInboundSdp 返回 Edge answer 且 claimId 必须匹配`() {
        val answer = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\na=candidate:1 1 udp 1 1.2.3.4 1 typ relay"
        server.enqueue(
            MockResponse().setResponseCode(202)
                .setBody(v2Claim.replace("\"sdpPending\":true", "\"sdpPending\":false")
                    .replace("\"transports\":[", "\"transports\":[{\"kind\":\"webrtc\",\"sdpAnswer\":\"$answer\",\"iceServers\":[{\"urls\":[\"turn:turn.example:3478\"],\"username\":\"u\",\"credential\":\"c\"}]},")),
        )
        server.enqueue(MockResponse().setResponseCode(202).setBody(v2Claim.replace("claim_abcdefghijkl", "claim_zzzzzzzzzzzz")))

        val session = client.completeInboundSdp("offer_abcdefghijkl", "claim_abcdefghijkl", "v=0\r\nfake")

        assertEquals("/v1/inbound-offers/offer_abcdefghijkl/media/sdp", server.takeRequest().path)
        assertTrue(session.directAttempted)
        assertFalse(session.sdpPending)
        assertTrue(session.sdpAnswer!!.contains("typ relay"))
        assertEquals(listOf("turn:turn.example:3478"), session.iceServers.single().urls)
        val mismatch = assertThrows(HostedCloudException::class.java) {
            client.completeInboundSdp("offer_abcdefghijkl", "claim_abcdefghijkl", "v=0\r\nfake")
        }
        assertEquals("INVALID_RESPONSE", mismatch.errorCode)
    }

    @Test
    fun `claim 带上响铃期预备的 SDP 时接受随 claim 返回的 answer`() {
        val answer = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\na=candidate:1 1 udp 1 1.2.3.4 1 typ relay"
        server.enqueue(
            MockResponse().setResponseCode(202)
                .setBody(v2Claim.replace("\"sdpPending\":true", "\"sdpPending\":false")
                    .replace("\"transports\":[", "\"transports\":[{\"kind\":\"webrtc\",\"sdpAnswer\":\"$answer\"},")),
        )

        val session = client.claimInboundOffer("offer_abcdefghijkl", sdpOffer = "v=0\r\nprepared", direct = true)

        val body = server.takeRequest().body.readUtf8()
        assertTrue(body.contains("\"mediaTransport\":\"webrtc\""))
        assertTrue(body.contains("\"sdpOffer\":\"v=0\\r\\nprepared\""))
        assertTrue(session.directAttempted)
        assertFalse(session.sdpPending)
        assertTrue(session.sdpAnswer!!.contains("typ relay"))
    }

    @Test
    fun `没发 offer 却收到 answer 视为协议破坏`() {
        server.enqueue(
            MockResponse().setResponseCode(202)
                .setBody(v2Claim.replace("\"sdpPending\":true", "\"sdpPending\":false")
                    .replace("\"transports\":[", "\"transports\":[{\"kind\":\"webrtc\",\"sdpAnswer\":\"v=0 bogus\"},")),
        )

        val error = assertThrows(HostedCloudException::class.java) { client.claimInboundOffer("offer_abcdefghijkl", direct = true) }
        assertEquals("INVALID_RESPONSE", error.errorCode)
    }

    @Test
    fun `直连响应自相矛盾或布尔不严格都拒绝`() {
        val cases = listOf(
            v2Claim.replace("\"directAttempted\":true", "\"directAttempted\":false"),
            v2Claim.replace("\"directAttempted\":true", "\"directAttempted\":\"true\""),
        )
        cases.forEach { server.enqueue(MockResponse().setResponseCode(202).setBody(it)) }
        server.enqueue(MockResponse().setResponseCode(202).setBody(v2Claim))

        cases.forEach {
            val error = assertThrows(HostedCloudException::class.java) { client.claimInboundOffer("offer_abcdefghijkl", direct = true) }
            assertEquals("INVALID_RESPONSE", error.errorCode)
        }
        // 没请求直连却收到直连字段同样不信。
        val plain = assertThrows(HostedCloudException::class.java) { client.claimInboundOffer("offer_abcdefghijkl") }
        assertEquals("INVALID_RESPONSE", plain.errorCode)
    }

    @Test
    fun `fallbackInboundLiveKit 与 endInboundClaim 走同一 claim`() {
        server.enqueue(MockResponse().setResponseCode(202).setBody(v2Claim.replace("\"sdpPending\":true", "\"sdpPending\":false")))
        server.enqueue(MockResponse().setResponseCode(202).setBody("""{"accepted":true,"offerId":"offer_abcdefghijkl","claimId":"claim_abcdefghijkl"}"""))

        val timing = """{"type":"client_timing","v":1,"attempt":"497f6eca-6276-4993-bfeb-53cbbbba6f08","seq":1,"marks":{"claim_start":0.0}}"""
        val fallback = client.fallbackInboundLiveKit("offer_abcdefghijkl", "claim_abcdefghijkl", listOf(timing))
        client.endInboundClaim("offer_abcdefghijkl", "claim_abcdefghijkl")

        assertEquals("a.b.c", fallback.token)
        val first = server.takeRequest()
        assertEquals("/v1/inbound-offers/offer_abcdefghijkl/media/livekit", first.path)
        val firstBody = first.body.readUtf8()
        assertTrue(firstBody.contains("\"claimId\":\"claim_abcdefghijkl\""))
        assertTrue(firstBody.contains("\"clientTiming\":[{\"type\":\"client_timing\""))
        assertEquals("/v1/inbound-offers/offer_abcdefghijkl/media/end", server.takeRequest().path)
    }

    @Test
    fun `inboundIceServers 解析 TURN 凭证且空列表报错`() {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setBody("""{"iceServers":[{"urls":["stun:stun.example:3478"]},{"urls":["turn:turn.example:3478?transport=udp"],"username":"u","credential":"c"}],"expiresAt":9999999999999}"""),
        )
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"iceServers":[],"expiresAt":1}"""))

        val servers = client.inboundIceServers("offer_abcdefghijkl")

        assertEquals("/v1/inbound-offers/offer_abcdefghijkl/ice", server.takeRequest().path)
        assertEquals(2, servers.size)
        assertEquals("c", servers[1].credential)
        assertFalse(servers[1].toString().contains("c\""))
        val empty = assertThrows(HostedCloudException::class.java) { client.inboundIceServers("offer_abcdefghijkl") }
        assertEquals("INVALID_RESPONSE", empty.errorCode)
    }

    @Test
    fun `listMessages consumes shared fixture with opaque cursor and no-store`() = runTest {
        client.credential = DeviceCredential("device_abcdefghijkl", "secret-value")
        server.enqueue(
            MockResponse().setResponseCode(200)
                .addHeader("Cache-Control", "no-store")
                .setBody(ContentTestFixtures.text("messages-page.json")),
        )

        val page = client.listMessages(25, "cursor_messages_fixture_0001")

        assertEquals(3, page.items.size)
        val request = server.takeRequest()
        assertEquals("/v1/messages?limit=25&cursor=cursor_messages_fixture_0001", request.path)
        assertEquals("no-store", request.getHeader("Cache-Control"))
        assertTrue(request.getHeader("Cookie")!!.startsWith("__Host-callpilot-device="))
    }

    @Test
    fun `listMessages rejects bad pagination before network and preserves stable error`() = runTest {
        assertThrows(HostedCloudException::class.java) {
            kotlinx.coroutines.runBlocking { client.listMessages(0, null) }
        }
        assertThrows(HostedCloudException::class.java) {
            kotlinx.coroutines.runBlocking { client.listMessages(25, "not-a-cursor") }
        }
        assertEquals(0, server.requestCount)

        server.enqueue(
            MockResponse().setResponseCode(413)
                .setBody("""{"error":{"code":"PAYLOAD_TOO_LARGE","message":"oversized"}}"""),
        )
        val error = assertThrows(HostedCloudException::class.java) {
            kotlinx.coroutines.runBlocking { client.listMessages(25, null) }
        }
        assertEquals("PAYLOAD_TOO_LARGE", error.errorCode)
    }

    @Test
    fun `listMessages rejects successful body above protocol limit`() = runTest {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setBody(Buffer().write(ByteArray(16_385) { ' '.code.toByte() })),
        )

        val error = assertThrows(HostedCloudException::class.java) {
            kotlinx.coroutines.runBlocking { client.listMessages(25, null) }
        }
        assertEquals("INVALID_RESPONSE", error.errorCode)
    }

    @Test
    fun `call content endpoints preserve paths cursors and shared fixtures`() = runTest {
        client.credential = DeviceCredential("device_abcdefghijkl", "secret-value")
        server.enqueue(MockResponse().setResponseCode(200).setBody(ContentTestFixtures.text("call-records-page.json")))
        server.enqueue(MockResponse().setResponseCode(200).setBody(ContentTestFixtures.text("call-record-detail-ready.json")))
        server.enqueue(MockResponse().setResponseCode(200).setBody(ContentTestFixtures.text("call-timeline-page.json")))

        val page = client.listCallRecords(25, "cursor_calls_fixture_0001")
        val callId = "call_fixture_pending_0001"
        val detail = client.getCallRecord(callId)
        val timeline = client.listCallTimeline(callId, 50, "cursor_timeline_fixture_0001")

        assertEquals(2, page.items.size)
        assertEquals(callId, detail.record.callId)
        assertEquals(5, timeline.items.size)
        assertEquals("/v1/call-records?limit=25&cursor=cursor_calls_fixture_0001", server.takeRequest().path)
        assertEquals("/v1/call-records/$callId", server.takeRequest().path)
        assertEquals(
            "/v1/call-records/$callId/timeline?limit=50&cursor=cursor_timeline_fixture_0001",
            server.takeRequest().path,
        )
    }

    @Test
    fun `call content rejects invalid identity before network and preserves stable oversized error`() = runTest {
        assertThrows(HostedCloudException::class.java) {
            kotlinx.coroutines.runBlocking { client.getCallRecord("local-directory-name") }
        }
        assertThrows(HostedCloudException::class.java) {
            kotlinx.coroutines.runBlocking { client.listCallTimeline("call_fixture_pending_0001", 0, null) }
        }
        assertEquals(0, server.requestCount)

        server.enqueue(
            MockResponse().setResponseCode(413)
                .setBody("""{"error":{"code":"PAYLOAD_TOO_LARGE","message":"oversized"}}"""),
        )
        val error = assertThrows(HostedCloudException::class.java) {
            kotlinx.coroutines.runBlocking { client.listCallRecords(25, null) }
        }
        assertEquals("PAYLOAD_TOO_LARGE", error.errorCode)
    }

    private val directCreated = """{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999999999999,"directAttempted":true,"transports":[{"kind":"webrtc","sdpAnswer":"v=0\r\nanswer"}]}"""

    @Test
    fun `createDirectCall 带 offer 建呼叫并解析 Edge answer`() {
        server.enqueue(MockResponse().setResponseCode(202).setBody(directCreated))

        val call = client.createDirectCall("edge_abcdefghijkl", "v=0\r\noffer", "android-idempotency-0001")

        assertEquals("call_abcdefghijkl", call.callId)
        assertTrue(call.directAttempted)
        assertEquals("v=0\r\nanswer", call.sdpAnswer)
        val request = server.takeRequest()
        assertEquals("/v1/calls", request.path)
        val body = request.body.readUtf8()
        assertTrue(body.contains("\"mediaTransport\":\"webrtc\""))
        assertTrue(body.contains("\"sdpOffer\""))
    }

    @Test
    fun `createDirectCall 预带 dialNumber 并校验号码格式`() {
        server.enqueue(MockResponse().setResponseCode(202).setBody(directCreated))
        client.createDirectCall("edge_abcdefghijkl", "v=0\r\noffer", "android-idempotency-0011", dialNumber = "+8610010086")

        val body = server.takeRequest().body.readUtf8()
        assertTrue(body.contains("\"dialNumber\":\"+8610010086\""))

        assertThrows(IllegalArgumentException::class.java) {
            client.createDirectCall("edge_abcdefghijkl", "v=0\r\noffer", "android-idempotency-0012", dialNumber = "10086-")
        }
    }

    @Test
    fun `createDirectCall 旧中转不走直连 Edge 拒绝带失败码 自相矛盾的响应不信`() {
        server.enqueue(MockResponse().setResponseCode(202)
            .setBody("""{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999999999999}"""))
        val legacy = client.createDirectCall("edge_abcdefghijkl", "v=0\r\noffer", "android-idempotency-0002")
        assertFalse(legacy.directAttempted)
        assertEquals(null, legacy.sdpAnswer)

        server.enqueue(MockResponse().setResponseCode(202)
            .setBody("""{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"failed","errorCode":"LINE_BUSY","createdAt":1,"expiresAt":9999999999999}"""))
        val busy = runCatching { client.createDirectCall("edge_abcdefghijkl", "v=0\r\noffer", "android-idempotency-0003") }
        assertEquals("LINE_BUSY", (busy.exceptionOrNull() as HostedCloudException).errorCode)

        server.enqueue(MockResponse().setResponseCode(202).setBody(directCreated.replace("\"directAttempted\":true", "\"directAttempted\":false")))
        val contradictory = runCatching { client.createDirectCall("edge_abcdefghijkl", "v=0\r\noffer", "android-idempotency-0004") }
        assertEquals("INVALID_RESPONSE", (contradictory.exceptionOrNull() as HostedCloudException).errorCode)
    }

    @Test
    fun `outboundIceServers 与 fallbackCallLiveKit 走外呼路由`() {
        server.enqueue(MockResponse().setResponseCode(200)
            .setBody("""{"iceServers":[{"urls":["turn:turn.example:3478"],"username":"u","credential":"c"}],"expiresAt":9999999999999}"""))
        server.enqueue(MockResponse().setResponseCode(202)
            .setBody("""{"callId":"call_abcdefghijkl","session":{"livekitUrl":"wss://cloud.livekit.example","token":"a.b.c","expiresAt":9999999999999}}"""))
        server.enqueue(MockResponse().setResponseCode(202)
            .setBody("""{"callId":"call_otherabcdefg","session":{"livekitUrl":"wss://cloud.livekit.example","token":"a.b.c","expiresAt":9999999999999}}"""))

        assertEquals("u", client.outboundIceServers("edge_abcdefghijkl").single().username)
        assertEquals("a.b.c", client.fallbackCallLiveKit("call_abcdefghijkl").token)
        val mismatch = runCatching { client.fallbackCallLiveKit("call_abcdefghijkl") }
        assertEquals("INVALID_RESPONSE", (mismatch.exceptionOrNull() as HostedCloudException).errorCode)
        assertEquals("/v1/calls/ice", server.takeRequest().path)
        assertEquals("/v1/calls/call_abcdefghijkl/media/livekit", server.takeRequest().path)
    }

    @Test
    fun `createDirectCall 重放时首次建呼叫仍在进行 等它 ready 再交回落`() {
        server.enqueue(MockResponse().setResponseCode(200)
            .setBody("""{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999999999999,"directAttempted":true}"""))
        server.enqueue(MockResponse().setResponseCode(200)
            .setBody("""{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999999999999,"directAttempted":true}"""))
        server.enqueue(MockResponse().setResponseCode(200)
            .setBody("""{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999999999999,"directAttempted":true}"""))

        val call = client.createDirectCall("edge_abcdefghijkl", "v=0\r\noffer", "android-idempotency-0005")

        assertTrue(call.directAttempted)
        assertEquals(null, call.sdpAnswer)
        assertEquals("/v1/calls", server.takeRequest().path)
        assertEquals("/v1/calls/call_abcdefghijkl", server.takeRequest().path)
        assertEquals("/v1/calls/call_abcdefghijkl", server.takeRequest().path)
        assertEquals(listOf(250L), sleeps)
    }

    @Test
    fun `createDirectCall 重放等待后中转已降级为普通 LiveKit 时不再标记直连`() {
        server.enqueue(MockResponse().setResponseCode(200)
            .setBody("""{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999999999999,"directAttempted":true}"""))
        server.enqueue(MockResponse().setResponseCode(200)
            .setBody("""{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999999999999,"session":{"livekitUrl":"wss://cloud.livekit.example","token":"a.b.c","expiresAt":9999999999999}}"""))

        val call = client.createDirectCall("edge_abcdefghijkl", "v=0\r\noffer", "android-idempotency-0006")

        assertFalse(call.directAttempted)
        assertEquals(null, call.sdpAnswer)
    }

    @Test
    fun `createDirectCall 重放等待到 failed 时保留服务端错误码`() {
        server.enqueue(MockResponse().setResponseCode(200)
            .setBody("""{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999999999999,"directAttempted":true}"""))
        server.enqueue(MockResponse().setResponseCode(200)
            .setBody("""{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"failed","errorCode":"EDGE_TIMEOUT","createdAt":1,"expiresAt":9999999999999,"directAttempted":true}"""))

        val error = assertThrows(HostedCloudException::class.java) {
            client.createDirectCall("edge_abcdefghijkl", "v=0\r\noffer", "android-idempotency-0007")
        }

        assertEquals("EDGE_TIMEOUT", error.errorCode)
    }
}
