package ai.bondings.callpilot.call

/** Local-only diagnostics. No free-form values other than filtered Server-Timing enter the log. */
class OutboundTiming(private val nowMs: () -> Double = { System.nanoTime() / 1_000_000.0 }) {
    enum class Mark(val wire: String) {
        START("start"), ICE_START("ice_start"), ICE_READY("ice_ready"),
        FACTORY_START("factory_start"), ADM("adm"), FACTORY("factory"), PEER_READY("peer_ready"),
        OFFER_CREATED("offer_created"), LOCAL_SET("local_set"),
        RELAY_SEEN("relay_seen"), ICE_COMPLETE("ice_complete"), GATHER_DONE("gather_done"),
        POST_START("post_start"), DNS_START("dns_start"), DNS_END("dns_end"),
        TCP_START("tcp_start"), TLS_START("tls_start"), TLS_END("tls_end"),
        CONNECTION("connection"), POST_SENT("post_sent"), RESPONSE_HEADERS("response_headers"), RESPONSE("response"),
    }

    private val origin = nowMs()
    private val marks = mutableMapOf<Mark, Long>()
    private var server = "none"
    private var attempts = 0

    @Synchronized fun mark(mark: Mark) {
        marks.putIfAbsent(mark, (nowMs() - origin).toLong().coerceAtLeast(0))
    }

    @Synchronized fun postAttempt() { attempts++ }

    @Synchronized fun serverTiming(header: String?) {
        val allowed = setOf("auth_checks", "presence", "call_insert", "sdp_relay", "edge_wait", "call_finalize_read")
        // Ignore descriptions, unknown metrics and malformed/non-finite durations entirely.
        server = header.orEmpty().take(4096).split(',').mapNotNull { metric ->
            val parts = metric.trim().split(';')
            val name = parts.first().trim()
            if (name !in allowed) return@mapNotNull null
            val value = parts.drop(1).firstOrNull { it.trim().startsWith("dur=") }
                ?.trim()?.removePrefix("dur=")?.toDoubleOrNull() ?: return@mapNotNull null
            if (!value.isFinite() || value !in 0.0..86_400_000.0) null else "$name:$value"
        }.distinct().joinToString(",").ifEmpty { "none" }
    }

    @Synchronized fun line(): String = buildString {
        append("outbound_timing tap=0")
        Mark.entries.forEach { append(' ').append(it.wire).append('=').append(marks[it] ?: -1) }
        append(" attempts=").append(attempts).append(" server_timing=").append(server)
    }
}
