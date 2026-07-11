package ai.bondings.callpilot.protocol

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class GatewayClientTest {

    private lateinit var server: MockWebServer
    private lateinit var client: GatewayClient

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        client = GatewayClient(server.url("/").toString())
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun expectedOrigin(): String = "http://${server.hostName}:${server.port}"

    @Test
    fun `pair 成功解析 Set-Cookie 凭证并携带 Origin`() {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .addHeader(
                    "Set-Cookie",
                    "__Host-callpilot-device=dev123.secret456; Path=/; HttpOnly; Secure",
                )
                .setBody(
                    """{"ok":true,"paired":true,"device":{"device_id":"dev123","display_name":"Pixel","created_at":1.0,"last_used_at":2.0}}"""
                ),
        )
        val result = client.pair("AB12-CD34", "Pixel")
        assertEquals("dev123", result.device.deviceId)
        assertEquals(DeviceCredential("dev123", "secret456"), result.credential)
        assertEquals(result.credential, client.credential)

        val req = server.takeRequest()
        assertEquals("/api/pair", req.path)
        assertEquals(expectedOrigin(), req.getHeader("Origin"))
        assertTrue(req.body.readUtf8().contains("AB12-CD34"))
    }

    @Test
    fun `pair 失败抛出网关错误信息`() {
        server.enqueue(
            MockResponse().setResponseCode(401)
                .setBody("""{"ok":false,"error":"配对码无效或已过期"}"""),
        )
        val e = assertThrows(GatewayException::class.java) { client.pair("XXXX-XXXX", "n") }
        assertEquals(401, e.statusCode)
        assertEquals("配对码无效或已过期", e.message)
    }

    @Test
    fun `createSession 携带设备 Cookie 并解析 invite`() {
        client.credential = DeviceCredential("dev123", "secret456")
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setBody(
                    """{"ok":true,"invite":{"session_id":"s-1","url":"https://d/p.html#frag","expires_at":1234.5}}"""
                ),
        )
        val invite = client.createSession()
        assertEquals("s-1", invite.sessionId)
        assertEquals(1234.5, invite.expiresAt, 0.0)

        val req = server.takeRequest()
        assertEquals("/api/session", req.path)
        assertEquals(
            "__Host-callpilot-device=dev123.secret456",
            req.getHeader("Cookie"),
        )
    }

    @Test
    fun `deviceStatus 未配对分支`() {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setBody("""{"ok":true,"paired":false,"edge":{"enabled":false,"configured":true}}"""),
        )
        val status = client.deviceStatus()
        assertFalse(status.paired)
        assertNull(status.device)
        assertFalse(status.edgeEnabled)
        assertTrue(status.edgeConfigured)
    }

    @Test
    fun `unpair 后清空本地凭证`() {
        client.credential = DeviceCredential("dev123", "secret456")
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"ok":true}"""))
        client.unpair()
        assertNull(client.credential)
    }

    @Test
    fun `非 JSON 响应转换为可读错误`() {
        server.enqueue(MockResponse().setResponseCode(502).setBody("Bad Gateway"))
        val e = assertThrows(GatewayException::class.java) { client.deviceStatus() }
        assertEquals(502, e.statusCode)
        assertTrue(e.message.contains("502"))
    }

    @Test
    fun `非回环 http 网关被拒绝`() {
        assertThrows(IllegalArgumentException::class.java) {
            GatewayClient("http://dial.example.com/")
        }
    }
}
