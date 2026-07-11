package ai.bondings.callpilot.protocol

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Before
import org.junit.Test

class PairingNegotiatorTest {
    private lateinit var server: MockWebServer

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() = server.shutdown()

    @Test
    fun `自动协商在 v1 路由 404 时回退同源 Tunnel`() {
        server.enqueue(MockResponse().setResponseCode(404).setBody("Not found"))
        server.enqueue(
            MockResponse().setResponseCode(200)
                .addHeader(
                    "Set-Cookie",
                    "__Host-callpilot-device=dev123.secret456; Path=/; Secure",
                )
                .setBody(
                    """{"ok":true,"paired":true,"device":{"device_id":"dev123","display_name":"Pixel"}}"""
                ),
        )

        val result = PairingNegotiator(server.url("/").toString()).pair("ABCD-EFGH", "Pixel")

        assertEquals(PairingProtocol.TUNNEL, result.protocol)
        assertEquals("/v1/pairing-sessions/claim", server.takeRequest().path)
        assertEquals("/api/pair", server.takeRequest().path)
    }

    @Test
    fun `自动协商 hosted 成功时不访问 Tunnel`() {
        server.enqueue(
            MockResponse().setResponseCode(201)
                .addHeader(
                    "Set-Cookie",
                    "__Host-callpilot-device=device_abcdefghijkl.secret-value; Path=/; Secure",
                )
                .setBody(
                    """{"paired":true,"device":{"deviceId":"device_abcdefghijkl","edgeId":"edge_abcdefghijkl","displayName":"Pixel"}}"""
                ),
        )

        val result = PairingNegotiator(server.url("/").toString()).pair("ABCD-EFGH", "Pixel")

        assertEquals(PairingProtocol.HOSTED, result.protocol)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `v1 业务错误不回退 Tunnel`() {
        server.enqueue(
            MockResponse().setResponseCode(401).setBody(
                """{"error":{"code":"INVALID_PAIRING","message":"expired","requestId":"req_1"}}"""
            ),
        )

        val error = assertThrows(HostedCloudException::class.java) {
            PairingNegotiator(server.url("/").toString()).pair("ABCD-EFGH", "Pixel")
        }

        assertEquals("INVALID_PAIRING", error.errorCode)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `手动选择 Tunnel 时直接使用 v0`() {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .addHeader(
                    "Set-Cookie",
                    "__Host-callpilot-device=dev123.secret456; Path=/; Secure",
                )
                .setBody(
                    """{"ok":true,"paired":true,"device":{"device_id":"dev123","display_name":"Pixel"}}"""
                ),
        )

        val result = PairingNegotiator(server.url("/").toString()).pair(
            "ABCD-EFGH",
            "Pixel",
            preferredProtocol = PairingProtocol.TUNNEL,
        )

        assertEquals(PairingProtocol.TUNNEL, result.protocol)
        assertEquals("/api/pair", server.takeRequest().path)
        assertEquals(1, server.requestCount)
    }
}
