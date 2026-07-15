package ai.bondings.callpilot.protocol

import java.io.IOException
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
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class HostedCloudClientTest {
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
}
