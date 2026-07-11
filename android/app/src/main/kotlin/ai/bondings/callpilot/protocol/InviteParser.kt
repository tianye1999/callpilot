package ai.bondings.callpilot.protocol

import java.util.Base64
import kotlinx.serialization.json.Json

/**
 * 解析邀请 URL 的 fragment：
 * - `#<base64url 无填充 JSON>` → [InvitePayload]（LiveKit 连接信息）
 * - `#pair=XXXX-XXXX` → 配对码深链
 */
object InviteParser {
    private val json = Json { ignoreUnknownKeys = true }

    fun fragmentOf(url: String): String = url.substringAfter('#', missingDelimiterValue = "")

    fun parsePairingCode(fragment: String): String? =
        fragment.takeIf { it.startsWith("pair=") }?.removePrefix("pair=")?.trim()
            ?.takeIf { it.isNotEmpty() }?.uppercase()

    fun parseInvitePayload(fragment: String): InvitePayload? {
        if (fragment.isEmpty() || fragment.startsWith("pair=") || fragment.length > 8192) return null
        return try {
            val bytes = Base64.getUrlDecoder().decode(fragment)
            json.decodeFromString<InvitePayload>(String(bytes, Charsets.UTF_8))
                .takeIf { it.v == 1 }
        } catch (_: Exception) {
            null
        }
    }

    fun parseInviteUrl(url: String): InvitePayload? = parseInvitePayload(fragmentOf(url))
}
