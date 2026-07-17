package ai.bondings.callpilot.content

import ai.bondings.callpilot.protocol.SMSMessage
import ai.bondings.callpilot.protocol.ContentWireValidation
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

@Serializable
data class MessageWatermark(
    val messageId: String,
    val occurredAt: Long,
)

@Serializable
data class MessageCacheSnapshot(
    val deviceId: String,
    val messages: List<SMSMessage>,
    val watermark: MessageWatermark?,
    val collectionRevision: String?,
    val savedAt: Long,
) {
    fun requireValid() {
        require(DEVICE_ID.matches(deviceId))
        messages.forEach(SMSMessage::requireValid)
        require(messages.map(SMSMessage::messageId).toSet().size == messages.size)
        require(watermark == null || (
            ContentWireValidation.validMessageId(watermark.messageId) && watermark.occurredAt >= 0
        ))
        require(collectionRevision == null || ContentWireValidation.validRevision(collectionRevision))
        require(savedAt >= 0)
    }

    companion object {
        private val DEVICE_ID = Regex("^device_[A-Za-z0-9_-]{12,80}$")
    }
}

interface MessageCacheStoring {
    fun load(deviceId: String): MessageCacheSnapshot?
    fun save(snapshot: MessageCacheSnapshot)
    fun clear()
}

class MessageCacheStore(
    private val protectedStore: ProtectedJsonStore,
    private val json: Json = Json { ignoreUnknownKeys = true },
) : MessageCacheStoring {
    override fun load(deviceId: String): MessageCacheSnapshot? {
        val bytes = protectedStore.read(deviceId) ?: return null
        return try {
            json.decodeFromString<MessageCacheSnapshot>(bytes.toString(Charsets.UTF_8))
                .also(MessageCacheSnapshot::requireValid)
                .takeIf { it.deviceId == deviceId }
                ?: run {
                    clear()
                    null
                }
        } catch (_: Exception) {
            clear()
            null
        }
    }

    override fun save(snapshot: MessageCacheSnapshot) {
        protectedStore.write(snapshot.deviceId, json.encodeToString(snapshot).encodeToByteArray())
    }

    override fun clear() = protectedStore.clear()
}
