package ai.bondings.callpilot.call

import ai.bondings.callpilot.protocol.MessageDeliveryStatus
import ai.bondings.callpilot.protocol.MessageDirection
import ai.bondings.callpilot.protocol.SMSMessage
import kotlinx.serialization.Serializable

@Serializable
data class SmsAlertHistory(val messageId: String, val revision: String)

@Serializable
data class SmsAlertState(
    val watermark: Long? = null,
    val notifiedMessages: List<SmsAlertHistory> = emptyList(),
)

data class SmsAlertAction(val message: SMSMessage, val silent: Boolean)

/** 水位只标记启用时间；revision 更新不改变首次提醒的顺序。 */
class SmsAlertPolicy(saved: SmsAlertState = SmsAlertState()) {
    private var watermark = saved.watermark
    private val notified = saved.notifiedMessages.distinctBy { it.messageId }.takeLast(MAX_HISTORY).toMutableList()

    fun pending(messages: List<SMSMessage>, now: Long): List<SmsAlertAction> {
        val since = watermark ?: run {
            watermark = now
            return emptyList()
        }
        return messages.distinctBy { it.messageId }.sortedBy { it.recordedAt }.mapNotNull { message ->
            if (message.direction != MessageDirection.INBOUND || message.status != MessageDeliveryStatus.RECEIVED) {
                return@mapNotNull null
            }
            val previous = notified.firstOrNull { it.messageId == message.messageId }
            when {
                previous != null && previous.revision != message.revision -> SmsAlertAction(message, silent = true)
                previous == null && message.recordedAt > since -> SmsAlertAction(message, silent = false)
                else -> null
            }
        }
    }

    fun notified(message: SMSMessage) {
        val entry = SmsAlertHistory(message.messageId, message.revision)
        val index = notified.indexOfFirst { it.messageId == message.messageId }
        if (index >= 0) notified[index] = entry else notified.add(entry)
        if (notified.size > MAX_HISTORY) notified.removeAt(0)
    }

    fun snapshot() = SmsAlertState(watermark, notified.toList())

    private companion object {
        const val MAX_HISTORY = 500
    }
}
