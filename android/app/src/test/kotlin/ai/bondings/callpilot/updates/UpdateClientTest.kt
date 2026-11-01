package ai.bondings.callpilot.updates

import java.io.IOException
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.*
import org.junit.Test

class UpdateClientTest {
    private val body = """{"product":"physical-sim","platform":"android","version":"0.7.6","publishedAt":1793491200,"notes":{"zh":"更新说明","en":"Release notes"},"downloadUrl":"https://sim-dl.bondings.ai/android/app.apk"}"""

    @Test fun `valid release parses both notes`() {
        val release = UpdateClient.parse(body)
        assertEquals("0.7.6", release.version)
        assertEquals("更新说明", release.notes.zh)
    }

    @Test fun `wrong product platform version URL and malformed schema are rejected`() {
        listOf("{}", "not json", body.replace("physical-sim", "other"), body.replace("android", "macos"),
            body.replace("0.7.6", "v0.7.6"), body.replace("https://", "http://"),
            body.replace("sim-dl.bondings.ai", "evil.example"), body.replace("1793491200", "-1"),
            body.replace("1793491200", "true"), body.replace("更新说明", ""),
            body.replace("\"en\":\"Release notes\"", "\"en\":42")).forEach {
            assertThrows(Exception::class.java) { UpdateClient.parse(it) }
        }
    }

    @Test fun `client performs public GET and rejects 404 and redirects`() {
        MockWebServer().use { server ->
            server.start()
            val client = UpdateClient(OkHttpClient(), server.url("/v1/releases/latest?product=physical-sim&platform=android").toString())
            server.enqueue(MockResponse().setBody(body))
            assertEquals("0.7.6", client.fetch().version)
            val request = server.takeRequest()
            assertEquals("GET", request.method)
            assertEquals("/v1/releases/latest?product=physical-sim&platform=android", request.path)
            assertNull(request.getHeader("Authorization"))
            assertNull(request.getHeader("Cookie"))
            server.enqueue(MockResponse().setResponseCode(404))
            assertThrows(Exception::class.java) { client.fetch() }
            server.enqueue(MockResponse().setResponseCode(302).addHeader("Location", server.url("/other")))
            assertThrows(Exception::class.java) { client.fetch() }
            assertEquals(3, server.requestCount)
        }
    }

    @Test fun `transport errors and oversized responses are rejected`() {
        val offline = OkHttpClient.Builder().addInterceptor { throw IOException("offline") }.build()
        assertThrows(IOException::class.java) { UpdateClient(offline).fetch() }
        MockWebServer().use { server ->
            server.start()
            server.enqueue(MockResponse().setBody(" ".repeat(65_537)))
            assertThrows(IOException::class.java) { UpdateClient(OkHttpClient(), server.url("/").toString()).fetch() }
        }
    }
}
