package ai.bondings.callpilot.protocol

import kotlinx.serialization.Serializable
import kotlinx.serialization.SerialName
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull

@Serializable
enum class CallDirection { INBOUND, OUTBOUND }

@JvmInline
@Serializable
value class CallRecordStatus(val rawValue: String) {
    companion object {
        val COMPLETED = CallRecordStatus("COMPLETED")
        val NOT_CONNECTED = CallRecordStatus("NOT_CONNECTED")
        val FAILED = CallRecordStatus("FAILED")
        val UNKNOWN = CallRecordStatus("UNKNOWN")
    }
}

@Serializable
enum class CallSource { AGENT, REMOTE_HANDSET, UNKNOWN }

@Serializable
enum class CallSummaryState { PENDING, READY, FAILED, UNAVAILABLE }

@Serializable
enum class CallTriageOutcome { AI_HANDLED, REJECTED, TRANSFERRED, UNKNOWN }

@Serializable
data class CallRecordItem(
    val callId: String,
    val revision: String,
    val direction: CallDirection,
    val address: String?,
    val startedAt: Long,
    val endedAt: Long?,
    val durationMs: Long?,
    val status: CallRecordStatus,
    val answered: Boolean,
    val source: CallSource,
    val summaryState: CallSummaryState,
    val summaryPreview: String?,
    val hasTranscript: Boolean,
    val triageOutcome: CallTriageOutcome?,
) {
    fun requireValid() {
        requireContentContract(ContentWireValidation.validCallId(callId))
        requireContentContract(ContentWireValidation.validRevision(revision))
        requireContentContract(ContentWireValidation.validProductCode(status.rawValue))
        requireContentContract(startedAt >= 0)
        requireContentContract(endedAt == null || endedAt >= startedAt)
        requireContentContract(durationMs == null || durationMs >= 0)
        requireContentContract((endedAt == null) == (durationMs == null))
    }
}

@Serializable
data class CallRecordsPage(
    val v: Int,
    val items: List<CallRecordItem>,
    val nextCursor: String?,
    val hasMore: Boolean,
    val collectionRevision: String,
    val oldestAvailableAt: Long?,
) {
    fun requireValid() {
        requireContentContract(v == 1 && items.size <= 100)
        items.forEach(CallRecordItem::requireValid)
        requireContentContract(items.map(CallRecordItem::callId).toSet().size == items.size)
        requireContentContract(hasMore == (nextCursor != null))
        requireContentContract(ContentWireValidation.validCursor(nextCursor))
        requireContentContract(ContentWireValidation.validRevision(collectionRevision))
        requireContentContract(oldestAvailableAt == null || oldestAvailableAt >= 0)
    }

    companion object {
        fun decode(json: Json, text: String): CallRecordsPage = decodeContent(json, text) { it.requireValid() }
    }
}

@Serializable
data class CallSummary(
    val ok: Boolean,
    val text: String?,
    val callerIdentity: String?,
    val intent: String?,
    val urgency: String?,
    val callbackNeeded: Boolean?,
    val errorCode: String?,
    val resultSource: String?,
    val resultVerification: String?,
)

@Serializable
data class CallRecordDetail(
    val v: Int,
    val record: CallRecordItem,
    val summary: CallSummary?,
    val timelineRevision: String,
) {
    fun requireValid() {
        requireContentContract(v == 1)
        record.requireValid()
        val summaryMustBeNil = record.summaryState in setOf(
            CallSummaryState.PENDING,
            CallSummaryState.UNAVAILABLE,
        )
        requireContentContract(summaryMustBeNil == (summary == null))
        requireContentContract(ContentWireValidation.validRevision(timelineRevision))
    }

    companion object {
        fun decode(json: Json, text: String): CallRecordDetail = decodeContent(json, text) { it.requireValid() }
    }
}

enum class CallTimelineKind { TRANSCRIPT, RESULT, TRIAGE, TAKEOVER, UNKNOWN }

@Serializable
enum class TimelineRole { AGENT, CALLER }

@Serializable
enum class TriageCategory { MARKETING, PERSONAL, NEEDS_OWNER, UNKNOWN }

@Serializable
enum class TriageAction { CLARIFY, CONTINUE_AI, REJECT, TRANSFER }

@Serializable
enum class TakeoverState { REQUESTED, COMMITTED, OWNER_HANGUP, FAILED }

@Serializable
data class CallTimelineItem(
    val timelineItemId: String,
    val occurredAt: Long,
    val type: String,
    @SerialName("role") private val roleValue: JsonElement? = null,
    @SerialName("text") private val textValue: JsonElement? = null,
    @SerialName("status") private val statusValue: JsonElement? = null,
    @SerialName("summary") private val summaryValue: JsonElement? = null,
    @SerialName("category") private val categoryValue: JsonElement? = null,
    @SerialName("action") private val actionValue: JsonElement? = null,
    @SerialName("confidence") private val confidenceValue: JsonElement? = null,
    @SerialName("reasonCode") private val reasonCodeValue: JsonElement? = null,
    @SerialName("state") private val stateValue: JsonElement? = null,
) {
    val kind: CallTimelineKind
        get() = when (type) {
            "TRANSCRIPT" -> CallTimelineKind.TRANSCRIPT
            "RESULT" -> CallTimelineKind.RESULT
            "TRIAGE" -> CallTimelineKind.TRIAGE
            "TAKEOVER" -> CallTimelineKind.TAKEOVER
            else -> CallTimelineKind.UNKNOWN
        }

    val timelineRole: TimelineRole? get() = TimelineRole.entries.firstOrNull { it.name == roleValue.stringOrNull() }
    val text: String? get() = textValue.stringOrNull()
    val recordStatus: CallRecordStatus? get() = statusValue.stringOrNull()?.let(::CallRecordStatus)
    val summary: String? get() = summaryValue.stringOrNull()
    val triageCategory: TriageCategory? get() = TriageCategory.entries.firstOrNull { it.name == categoryValue.stringOrNull() }
    val triageAction: TriageAction? get() = TriageAction.entries.firstOrNull { it.name == actionValue.stringOrNull() }
    val confidence: Double? get() = (confidenceValue as? JsonPrimitive)?.takeIf { !it.isString }?.doubleOrNull
    val reasonCode: String? get() = reasonCodeValue.stringOrNull()
    val takeoverState: TakeoverState? get() = TakeoverState.entries.firstOrNull { it.name == stateValue.stringOrNull() }

    fun requireValid() {
        requireContentContract(ContentWireValidation.validTimelineItemId(timelineItemId))
        requireContentContract(occurredAt >= 0)
        requireContentContract(ContentWireValidation.validProductCode(type))
        when (kind) {
            CallTimelineKind.TRANSCRIPT -> requireContentContract(timelineRole != null && text != null)
            CallTimelineKind.RESULT -> requireContentContract(
                recordStatus != null && ContentWireValidation.validProductCode(recordStatus!!.rawValue),
            )
            CallTimelineKind.TRIAGE -> {
                val parsedConfidence = confidence
                val parsedReasonCode = reasonCode
                requireContentContract(
                    triageCategory != null && triageAction != null && parsedConfidence != null &&
                        parsedConfidence in 0.0..1.0 && parsedReasonCode != null &&
                        ContentWireValidation.validProductCode(parsedReasonCode),
                )
            }
            CallTimelineKind.TAKEOVER -> {
                val parsedReasonCode = reasonCode
                requireContentContract(
                    takeoverState != null &&
                        (parsedReasonCode == null || ContentWireValidation.validProductCode(parsedReasonCode)),
                )
            }
            CallTimelineKind.UNKNOWN -> Unit
        }
    }
}

private fun JsonElement?.stringOrNull(): String? =
    (this as? JsonPrimitive)?.takeIf(JsonPrimitive::isString)?.content

@Serializable
data class CallTimelinePage(
    val v: Int,
    val items: List<CallTimelineItem>,
    val nextCursor: String?,
    val hasMore: Boolean,
    val collectionRevision: String,
    val oldestAvailableAt: Long?,
) {
    val visibleItems: List<CallTimelineItem> get() = items.filter { it.kind != CallTimelineKind.UNKNOWN }

    fun requireValid() {
        requireContentContract(v == 1 && items.size <= 100)
        items.forEach(CallTimelineItem::requireValid)
        requireContentContract(items.map(CallTimelineItem::timelineItemId).toSet().size == items.size)
        requireContentContract(hasMore == (nextCursor != null))
        requireContentContract(ContentWireValidation.validCursor(nextCursor))
        requireContentContract(ContentWireValidation.validRevision(collectionRevision))
        requireContentContract(oldestAvailableAt == null || oldestAvailableAt >= 0)
    }

    companion object {
        fun decode(json: Json, text: String): CallTimelinePage = decodeContent(json, text) { it.requireValid() }
    }
}

interface CallRecordContentClient {
    suspend fun listCallRecords(limit: Int, cursor: String?): CallRecordsPage
    suspend fun getCallRecord(callId: String): CallRecordDetail
    suspend fun listCallTimeline(callId: String, limit: Int, cursor: String?): CallTimelinePage
}

private inline fun <reified T> decodeContent(json: Json, text: String, validate: (T) -> Unit): T = try {
    json.decodeFromString<T>(text).also(validate)
} catch (error: ContentContractException) {
    throw error
} catch (error: SerializationException) {
    throw ContentContractException(error)
} catch (error: IllegalArgumentException) {
    throw ContentContractException(error)
}

private fun requireContentContract(condition: Boolean) {
    if (!condition) throw ContentContractException()
}
