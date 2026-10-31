package ai.bondings.callpilot.call

import ai.bondings.callpilot.protocol.CallDirection
import ai.bondings.callpilot.protocol.CallRecordItem
import ai.bondings.callpilot.protocol.CallRecordStatus
import ai.bondings.callpilot.protocol.CallSource
import ai.bondings.callpilot.protocol.CallSummaryState
import kotlinx.serialization.Serializable

@Serializable
data class CallDigestState(
    val watermark: Long? = null,
    val notifiedCallIds: List<String> = emptyList(),
)

/** 水位只标记启用时间，不随查询推进，避免漏掉摘要尚未就绪的记录。 */
class CallDigestPolicy(saved: CallDigestState = CallDigestState()) {
    private var watermark = saved.watermark
    private val notified = saved.notifiedCallIds.distinct().takeLast(MAX_HISTORY).toMutableList()

    fun pending(records: List<CallRecordItem>, now: Long): List<CallRecordItem> {
        val since = watermark ?: run {
            watermark = now
            return emptyList()
        }
        return records.distinctBy { it.callId }.filter { record ->
            record.direction == CallDirection.INBOUND && record.endedAt != null &&
                record.source != CallSource.REMOTE_HANDSET && record.startedAt > since &&
                record.callId !in notified &&
                (record.summaryState != CallSummaryState.PENDING || now - record.endedAt >= SUMMARY_WAIT_MS)
        }.sortedBy { it.startedAt }
    }

    fun notified(callId: String) {
        if (callId in notified) return
        notified.add(callId)
        if (notified.size > MAX_HISTORY) notified.removeAt(0)
    }

    fun snapshot() = CallDigestState(watermark, notified.toList())

    companion object {
        private const val MAX_HISTORY = 200
        private const val SUMMARY_WAIT_MS = 180_000L

        fun isMissed(record: CallRecordItem): Boolean =
            !record.answered || record.status != CallRecordStatus.COMPLETED
    }
}
