package ai.bondings.callpilot.updates

import java.net.URI

/** Shared by foreground and service checks; timestamps use elapsedRealtime, not wall time. */
class UpdatePolicy(notifiedVersions: Set<String> = emptySet()) {
    private val notified = notifiedVersions.mapNotNull(::versionKey).toMutableSet()
    private var lastCheck: Long? = null

    fun beginCheck(now: Long, manual: Boolean = false): Boolean {
        if (!manual && lastCheck?.let { now - it < INTERVAL_MS } == true) return false
        lastCheck = now
        return true
    }

    fun shouldNotify(version: String): Boolean = versionKey(version)?.let { it !in notified } == true

    fun notified(version: String) { versionKey(version)?.let { notified.add(it) } }

    fun snapshot(): Set<String> = notified.toSet()

    fun nextCheckDelay(now: Long): Long = lastCheck?.let { (INTERVAL_MS - (now - it)).coerceAtLeast(1) } ?: 1

    companion object {
        const val INTERVAL_MS = 6 * 60 * 60 * 1000L
        private val versionPattern = Regex("[0-9]{1,9}(?:\\.[0-9]{1,9}){1,7}")

        fun versionParts(value: String): List<Int>? =
            value.takeIf { versionPattern.matches(it) }?.split('.')?.map(String::toInt)

        private fun versionKey(value: String): String? = versionParts(value)?.let { parts ->
            // Keep at least two segments so persisted keys remain valid versions.
            parts.dropLastWhile { it == 0 }.let { it + List((2 - it.size).coerceAtLeast(0)) { 0 } }
                .joinToString(".")
        }

        fun isNewer(candidate: String, current: String): Boolean {
            val new = versionParts(candidate) ?: return false
            val old = versionParts(current) ?: return false
            for (index in 0 until maxOf(new.size, old.size)) {
                val comparison = (new.getOrElse(index) { 0 }).compareTo(old.getOrElse(index) { 0 })
                if (comparison != 0) return comparison > 0
            }
            return false
        }

        fun validDownloadUrl(value: String): Boolean {
            if (value.any { it.isWhitespace() || it.code < 32 || it == '\\' }) return false
            return runCatching {
                val uri = URI(value)
                uri.scheme.equals("https", ignoreCase = true) &&
                    uri.host.equals("sim-dl.bondings.ai", ignoreCase = true) &&
                    uri.rawUserInfo == null && uri.rawFragment == null &&
                    (uri.port == -1 || uri.port in 1..65535)
            }.getOrDefault(false)
        }
    }
}
