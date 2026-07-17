package ai.bondings.callpilot.content

import ai.bondings.callpilot.protocol.CallRecordDetail
import ai.bondings.callpilot.protocol.CallRecordItem
import ai.bondings.callpilot.protocol.CallTimelineItem
import ai.bondings.callpilot.protocol.ContentWireValidation
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

@Serializable
data class CachedCallDetail(
    val detail: CallRecordDetail,
    val timeline: List<CallTimelineItem>,
    val nextTimelineCursor: String?,
    val timelineCollectionRevision: String?,
) {
    fun requireValid() {
        detail.requireValid()
        timeline.forEach(CallTimelineItem::requireValid)
        require(timeline.map(CallTimelineItem::timelineItemId).toSet().size == timeline.size)
        require(ContentWireValidation.validCursor(nextTimelineCursor))
        require(
            timelineCollectionRevision == null ||
                ContentWireValidation.validRevision(timelineCollectionRevision),
        )
    }
}

@Serializable
data class CallHistoryCacheSnapshot(
    val deviceId: String,
    val records: List<CallRecordItem>,
    val collectionRevision: String?,
    val details: Map<String, CachedCallDetail>,
    val savedAt: Long,
) {
    fun requireValid() {
        require(DEVICE_ID.matches(deviceId))
        records.forEach(CallRecordItem::requireValid)
        require(records.map(CallRecordItem::callId).toSet().size == records.size)
        require(collectionRevision == null || ContentWireValidation.validRevision(collectionRevision))
        details.forEach { (callId, cached) ->
            require(callId == cached.detail.record.callId && ContentWireValidation.validCallId(callId))
            cached.requireValid()
        }
        require(savedAt >= 0)
    }

    companion object {
        private val DEVICE_ID = Regex("^device_[A-Za-z0-9_-]{12,80}$")
    }
}

interface CallHistoryCacheStoring {
    fun load(deviceId: String): CallHistoryCacheSnapshot?
    fun save(snapshot: CallHistoryCacheSnapshot)
    fun clear()
}

class CallHistoryCacheStore(
    private val protectedStore: ProtectedJsonStore,
    private val json: Json = Json { ignoreUnknownKeys = true },
) : CallHistoryCacheStoring {
    override fun load(deviceId: String): CallHistoryCacheSnapshot? {
        val bytes = protectedStore.read(deviceId) ?: return null
        return try {
            json.decodeFromString<CallHistoryCacheSnapshot>(bytes.toString(Charsets.UTF_8))
                .also(CallHistoryCacheSnapshot::requireValid)
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

    override fun save(snapshot: CallHistoryCacheSnapshot) {
        snapshot.requireValid()
        protectedStore.write(snapshot.deviceId, json.encodeToString(snapshot).encodeToByteArray())
    }

    override fun clear() = protectedStore.clear()
}
