package ai.bondings.callpilot.updates

import ai.bondings.callpilot.protocol.HttpClients
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import okhttp3.Request

@Serializable
data class ReleaseNotes(val zh: String, val en: String)

@Serializable
data class AppRelease(
    val product: String,
    val platform: String,
    val version: String,
    val publishedAt: Double,
    val notes: ReleaseNotes,
    val downloadUrl: String,
)

class UpdateClient(
    client: OkHttpClient = HttpClients.shared,
    private val endpoint: String = ENDPOINT,
) {
    private val http = client.newBuilder().followRedirects(false).followSslRedirects(false)
        .callTimeout(20, TimeUnit.SECONDS).build()

    fun fetch(): AppRelease {
        val request = Request.Builder().url(endpoint).header("Cache-Control", "no-store").get().build()
        return http.newCall(request).execute().use { response ->
            if (response.code != 200) throw IOException("Release HTTP ${response.code}")
            val source = response.body?.source() ?: throw IOException("Missing release body")
            if (source.request(MAX_BODY_BYTES + 1)) throw IOException("Release body too large")
            parse(source.readString(Charsets.UTF_8))
        }
    }

    companion object {
        const val ENDPOINT = "https://sim-api.bondings.ai/v1/releases/latest?product=physical-sim&platform=android"
        private const val MAX_BODY_BYTES = 64 * 1024L
        val json = Json { ignoreUnknownKeys = true }

        fun parse(body: String): AppRelease = json.decodeFromString<AppRelease>(body).also {
            require(it.product == "physical-sim" && it.platform == "android")
            require(UpdatePolicy.versionParts(it.version) != null)
            require(it.publishedAt.isFinite() && it.publishedAt > 0)
            require(it.notes.zh.isNotBlank() && it.notes.en.isNotBlank())
            require(UpdatePolicy.validDownloadUrl(it.downloadUrl))
        }
    }
}
