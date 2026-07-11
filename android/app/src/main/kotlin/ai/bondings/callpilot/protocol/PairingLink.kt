package ai.bondings.callpilot.protocol

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/**
 * 配对输入解析：桌面端的配对链接 `https://<网关>/remote_dialer.html#pair=XXXX-XXXX`
 * 同时携带网关 origin 与配对码（静态页与 /api 接口同源，见 docs/remote-protocol.md），
 * 因此用户只需粘贴链接，无需单独填网关地址。也兼容裸配对码。
 */
object PairingLink {

    data class Parsed(val gatewayBase: String?, val code: String?) {
        val isEmpty: Boolean get() = gatewayBase == null && code == null
    }

    private val BARE_CODE_RE = Regex("^[A-Za-z0-9]{4}-?[A-Za-z0-9]{4}$")

    /** 接受完整配对链接 / 裸配对码；识别不出返回空 Parsed。 */
    fun parse(text: String): Parsed {
        val t = text.trim()
        if (t.isEmpty()) return Parsed(null, null)
        if (BARE_CODE_RE.matches(t)) return Parsed(null, normalizeCode(t))
        val url = t.toHttpUrlOrNull() ?: return Parsed(null, null)
        // 网关凭证走 __Host- Cookie，只在 https 下有效；明文链接一律不识别
        if (!url.isHttps) return Parsed(null, null)
        val code = InviteParser.parsePairingCode(url.fragment.orEmpty())?.let { normalizeCode(it) }
        return Parsed(originOf(url), code)
    }

    /** 去横线、大写，仅保留 8 位字母数字；不足 8 位返回 null。 */
    fun normalizeCode(raw: String): String? {
        val cleaned = raw.filter { it.isLetterOrDigit() }.uppercase()
        return cleaned.takeIf { it.length == 8 }
    }

    /** 展示/提交用 `ABCD-EFGH` 格式。 */
    fun formatCode(normalized: String): String =
        "${normalized.take(4)}-${normalized.drop(4)}"

    private fun originOf(url: HttpUrl): String = buildString {
        append(url.scheme).append("://").append(url.host)
        if (url.port != HttpUrl.defaultPort(url.scheme)) append(":").append(url.port)
    }
}
