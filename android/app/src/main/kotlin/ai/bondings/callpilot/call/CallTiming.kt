package ai.bondings.callpilot.call

import java.util.UUID
import kotlinx.coroutines.sync.Mutex
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/**
 * 一通的客户端里程碑（与 iOS CallTiming、Edge `call_timing.CLIENT_MARKS` 同一套名称）：只记相对本通起点的毫秒数，
 * 不含号码、地址或任何内容。经控制通道分批发给 Edge（每批 ≤16 个、≤2 KiB、seq 递增），Edge 写进本通 events.jsonl，
 * 真机通话后不用再让机主复述「什么时候听到声音」。
 */
class CallTiming(private val nowMs: () -> Double = { System.nanoTime() / 1_000_000.0 }) {
    enum class Mark(val wire: String) {
        ANSWER_ACTION("answer_action"), DIAL_START("dial_start"),
        CLAIM_START("claim_start"), CLAIM_RESPONSE("claim_response"),
        SDP_SUBMIT_START("sdp_submit_start"), SDP_SUBMIT_RESPONSE("sdp_submit_response"),
        PREPARE_START("prepare_start"), PREPARE_RESPONSE("prepare_response"),
        PRECONNECT_START("preconnect_start"), PRECONNECT_READY("preconnect_ready"),
        COLD_FALLBACK("cold_fallback"),
        CONNECT_START("connect_start"), CONNECT_RETURN("connect_return"),
        ROOM_CONNECTED("room_connected"), AUDIO_ACTIVATED("audio_activated"),
        DIRECT_CONTROL_READY("direct_control_ready"),
        DOWNLINK_FIRST_FRAME("downlink_first_frame"), EDGE_CONNECTED("edge_connected"),
        HANGUP_INTENT("hangup_intent"), HANGUP_ACK("hangup_ack"), DISCONNECT("disconnect"),
    }

    /** 一批待发里程碑：发送失败时按 marks 退回，下次重发。 */
    class Batch(val text: String, internal val marks: List<Mark>)

    val attempt: String = UUID.randomUUID().toString()

    /** 同一通的批次串行发送：Edge 只收更大的 seq，并发发送若乱序会丢掉较小的那批。 */
    val sendLock = Mutex()
    private var origin = nowMs()
    private val marks = LinkedHashMap<Mark, Double>()
    private val sent = HashSet<Mark>()
    var sequence = 0
        private set

    @Synchronized
    fun mark(mark: Mark, atMs: Double? = null) {
        if (mark !in marks) marks[mark] = atMs ?: nowMs()
    }

    /**
     * 以此刻为本通起点并记下该里程碑：外呼是 dial_start；来电先记 answer_action、认领时以 claim_start 重设
     * （与 iOS startClaim 相同，所以 answer_action 通常是负数）。Edge 只拿同一通里两个里程碑相减，不受起点影响。
     */
    @Synchronized
    fun restartAt(mark: Mark) {
        origin = nowMs()
        marks[mark] = origin
    }

    /** 下一批未发出的里程碑；没有新内容或已到 seq 上限时返回 null。 */
    @Synchronized
    fun nextBatch(): Batch? {
        if (sequence >= MAX_BATCHES) return null
        val pending = Mark.entries.filter { it in marks && it !in sent }.take(MAX_BATCH_MARKS)
        if (pending.isEmpty()) return null
        val text = buildJsonObject {
            put("type", "client_timing")
            put("v", 1)
            put("attempt", attempt)
            put("seq", sequence + 1)
            putJsonObject("marks") {
                for (mark in pending) {
                    val ms = ((marks.getValue(mark) - origin) * 1000).let { Math.round(it) / 1000.0 }
                    put(mark.wire, ms.coerceIn(-60_000.0, 86_400_000.0))
                }
            }
        }.toString()
        if (text.toByteArray().size > MAX_BATCH_BYTES) return null
        sequence += 1
        sent += pending
        return Batch(text, pending)
    }

    /** 控制通道发送失败：这些里程碑下次重发（seq 仍递增，Edge 只收更大的 seq）。 */
    @Synchronized
    fun unsend(batch: Batch) {
        sent -= batch.marks.toSet()
    }

    companion object {
        const val MAX_BATCH_MARKS = 16
        const val MAX_BATCH_BYTES = 2048
        const val MAX_BATCHES = 64
    }
}
