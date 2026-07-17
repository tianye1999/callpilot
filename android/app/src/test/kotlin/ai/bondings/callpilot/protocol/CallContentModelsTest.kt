package ai.bondings.callpilot.protocol

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class CallContentModelsTest {
    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `shared call fixtures preserve summary lifecycle and remote handset empty state`() {
        val records = CallRecordsPage.decode(json, ContentTestFixtures.text("call-records-page.json"))
        val pending = CallRecordDetail.decode(json, ContentTestFixtures.text("call-record-detail-pending.json"))
        val ready = CallRecordDetail.decode(json, ContentTestFixtures.text("call-record-detail-ready.json"))
        val remote = CallRecordDetail.decode(json, ContentTestFixtures.text("call-record-detail-no-transcript.json"))

        assertEquals(2, records.items.size)
        assertEquals(CallSummaryState.PENDING, pending.record.summaryState)
        assertNull(pending.summary)
        assertEquals(CallSummaryState.READY, ready.record.summaryState)
        assertEquals(true, ready.summary?.ok)
        assertEquals(CallSource.REMOTE_HANDSET, remote.record.source)
        assertFalse(remote.record.hasTranscript)
        assertEquals(CallSummaryState.UNAVAILABLE, remote.record.summaryState)
        assertNull(remote.summary)
    }

    @Test
    fun `timeline fixture stays oldest first and exposes only known product events`() {
        val page = CallTimelinePage.decode(json, ContentTestFixtures.text("call-timeline-page.json"))

        assertEquals(5, page.items.size)
        assertTrue(page.items.zipWithNext().all { (left, right) -> left.occurredAt <= right.occurredAt })
        assertEquals(CallTimelineKind.TRANSCRIPT, page.items.first().kind)
        assertEquals(TimelineRole.AGENT, page.items.first().timelineRole)
        assertEquals(CallTimelineKind.RESULT, page.items.last().kind)

        val withFutureType = ContentTestFixtures.text("call-timeline-page.json")
            .replaceFirst("\"TRANSCRIPT\"", "\"FUTURE_EVENT\"")
            .replaceFirst("\"role\": \"AGENT\"", "\"role\": {\"future\": true}")
        val future = CallTimelinePage.decode(json, withFutureType)
        assertEquals(CallTimelineKind.UNKNOWN, future.items.first().kind)
        assertEquals(4, future.visibleItems.size)
    }

    @Test
    fun `call identity timestamp and summary invariants fail closed`() {
        val invalidId = ContentTestFixtures.text("call-records-page.json")
            .replaceFirst("call_fixture_agent_0001", "local-directory-name")
        assertThrows(ContentContractException::class.java) {
            CallRecordsPage.decode(json, invalidId)
        }

        val booleanTime = ContentTestFixtures.text("call-records-page.json")
            .replaceFirst("1784161000000", "true")
        assertThrows(ContentContractException::class.java) {
            CallRecordsPage.decode(json, booleanTime)
        }

        val pendingWithSummary = ContentTestFixtures.text("call-record-detail-ready.json")
            .replaceFirst("\"summaryState\": \"READY\"", "\"summaryState\": \"PENDING\"")
        assertThrows(ContentContractException::class.java) {
            CallRecordDetail.decode(json, pendingWithSummary)
        }

        val invalidConfidence = ContentTestFixtures.text("call-timeline-page.json")
            .replace("\"confidence\": 0.94", "\"confidence\": 1.5")
        assertThrows(ContentContractException::class.java) {
            CallTimelinePage.decode(json, invalidConfidence)
        }
    }
}
