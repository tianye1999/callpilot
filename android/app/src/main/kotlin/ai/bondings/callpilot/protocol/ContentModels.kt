package ai.bondings.callpilot.protocol

import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json

@Serializable
enum class MessageDirection { INBOUND, OUTBOUND }

@Serializable
enum class MessageDeliveryStatus { RECEIVED, SENT, FAILED, ERROR }

@Serializable
data class SMSMessage(
    val messageId: String,
    val revision: String,
    val direction: MessageDirection,
    val address: String,
    val text: String,
    val occurredAt: Long,
    val recordedAt: Long,
    val status: MessageDeliveryStatus,
) {
    fun requireValid() {
        checkContract(ContentWireValidation.validMessageId(messageId))
        checkContract(ContentWireValidation.validRevision(revision))
        checkContract(occurredAt >= 0 && recordedAt >= 0)
        checkContract(status != MessageDeliveryStatus.RECEIVED || direction == MessageDirection.INBOUND)
    }
}

@Serializable
data class MessagePage(
    val v: Int,
    val items: List<SMSMessage>,
    val nextCursor: String?,
    val hasMore: Boolean,
    val collectionRevision: String,
    val oldestAvailableAt: Long?,
) {
    fun requireValid() {
        checkContract(v == 1)
        checkContract(items.size <= 100)
        items.forEach(SMSMessage::requireValid)
        checkContract(items.map(SMSMessage::messageId).toSet().size == items.size)
        checkContract(hasMore == (nextCursor != null))
        checkContract(ContentWireValidation.validCursor(nextCursor))
        checkContract(ContentWireValidation.validRevision(collectionRevision))
        checkContract(oldestAvailableAt == null || oldestAvailableAt >= 0)
    }

    companion object {
        fun decode(json: Json, text: String): MessagePage = try {
            json.decodeFromString<MessagePage>(text).also(MessagePage::requireValid)
        } catch (error: ContentContractException) {
            throw error
        } catch (error: SerializationException) {
            throw ContentContractException(error)
        } catch (error: IllegalArgumentException) {
            throw ContentContractException(error)
        }
    }
}

interface MessageContentClient {
    suspend fun listMessages(limit: Int, cursor: String?): MessagePage
}

object ContentWireValidation {
    private val CURSOR = Regex("^cursor_[A-Za-z0-9_-]+$")
    private val MESSAGE_ID = Regex("^msg_[A-Za-z0-9_-]{12,80}$")
    private val CALL_ID = Regex("^call_[A-Za-z0-9_-]{12,80}$")
    private val TIMELINE_ITEM_ID = Regex("^item_[A-Za-z0-9_-]{12,80}$")
    private val REVISION = Regex("^revision_[A-Za-z0-9_-]{12,80}$")
    private val PRODUCT_CODE = Regex("^[A-Z][A-Z0-9_]{2,63}$")

    fun validCursor(value: String?): Boolean =
        value == null || (value.length <= 2_048 && CURSOR.matches(value))

    fun validRevision(value: String): Boolean = REVISION.matches(value)

    fun validMessageId(value: String): Boolean = MESSAGE_ID.matches(value)

    fun validCallId(value: String): Boolean = CALL_ID.matches(value)

    fun validTimelineItemId(value: String): Boolean = TIMELINE_ITEM_ID.matches(value)

    fun validProductCode(value: String): Boolean = PRODUCT_CODE.matches(value)
}

class ContentContractException(cause: Throwable? = null) : Exception("Invalid content-sync payload", cause)

private fun checkContract(condition: Boolean) {
    if (!condition) throw ContentContractException()
}
