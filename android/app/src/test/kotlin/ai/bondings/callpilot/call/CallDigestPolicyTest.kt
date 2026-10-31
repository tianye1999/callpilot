package ai.bondings.callpilot.call

import ai.bondings.callpilot.protocol.*
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Test

class CallDigestPolicyTest {
    private val now = 1_000_000L

    @Test
    fun `first run records current time without replaying history`() {
        val policy = CallDigestPolicy()
        assertTrue(policy.pending(listOf(record()), now).isEmpty())
        assertEquals(now, policy.snapshot().watermark)
        assertTrue(policy.pending(listOf(record(startedAt = now)), now + 1).isEmpty())
        assertEquals(1, policy.pending(listOf(record(startedAt = now + 1)), now + 2).size)
    }

    @Test
    fun `only ended inbound calls not answered on handset qualify regardless of triage`() {
        val policy = initialized()
        assertTrue(policy.pending(listOf(
            record().copy(direction = CallDirection.OUTBOUND),
            record().copy(source = CallSource.REMOTE_HANDSET),
            record().copy(endedAt = null),
        ), now).isEmpty())
        for (source in listOf(CallSource.AGENT, CallSource.UNKNOWN)) {
            for (triage in CallTriageOutcome.entries + listOf(null)) {
                assertEquals(1, policy.pending(listOf(record().copy(source = source, triageOutcome = triage)), now).size)
            }
        }
    }

    @Test
    fun `notified IDs survive restore and repeated list entries are deduplicated`() {
        val policy = initialized()
        assertEquals(1, policy.pending(listOf(record(), record()), now).size)
        policy.notified(record().callId)
        val restored = CallDigestPolicy(policy.snapshot())
        assertTrue(restored.pending(listOf(record()), now).isEmpty())
    }

    @Test
    fun `pending summary waits until exactly three minutes after end`() {
        val policy = initialized()
        val pending = record().copy(summaryState = CallSummaryState.PENDING, endedAt = now - 179_999)
        assertTrue(policy.pending(listOf(pending), now).isEmpty())
        assertEquals(1, policy.pending(listOf(pending), now + 1).size)
        assertEquals(1, policy.pending(listOf(pending.copy(summaryState = CallSummaryState.READY)), now).size)
    }

    @Test
    fun `failed or unavailable summaries do not suppress notification`() {
        for (state in listOf(CallSummaryState.FAILED, CallSummaryState.UNAVAILABLE)) {
            assertEquals(1, initialized().pending(listOf(record().copy(summaryState = state)), now).size)
        }
    }

    @Test
    fun `later polls and process restore do not advance past pending calls`() {
        val policy = initialized()
        val pending = record().copy(summaryState = CallSummaryState.PENDING, endedAt = now)
        assertTrue(policy.pending(listOf(pending), now).isEmpty())
        val saved = Json.encodeToString(policy.snapshot())
        val restored = CallDigestPolicy(Json.decodeFromString<CallDigestState>(saved))
        restored.pending(emptyList(), now + 30_000)
        assertEquals(listOf(pending), restored.pending(listOf(pending), now + 180_000))
        assertEquals(1L, restored.snapshot().watermark)
    }

    @Test
    fun `failed notification does not consume candidate until explicitly notified`() {
        val policy = initialized()
        assertEquals(listOf(record()), policy.pending(listOf(record()), now))
        assertEquals(listOf(record()), policy.pending(listOf(record()), now + 30_000))
        policy.notified(record().callId)
        assertTrue(policy.pending(listOf(record()), now + 60_000).isEmpty())
    }

    @Test
    fun `call started before watermark is ignored even if it ends after first run`() {
        val policy = CallDigestPolicy()
        policy.pending(emptyList(), now)
        val old = record(startedAt = now - 1).copy(endedAt = now + 10)
        assertTrue(policy.pending(listOf(old), now + 11).isEmpty())
    }

    @Test
    fun `history keeps newest 200 unique IDs in notification order`() {
        val policy = initialized()
        repeat(201) { policy.notified("call_test_$it") }
        policy.notified("call_test_200")
        assertEquals(200, policy.snapshot().notifiedCallIds.size)
        assertEquals("call_test_1", policy.snapshot().notifiedCallIds.first())
        assertEquals("call_test_200", policy.snapshot().notifiedCallIds.last())
    }

    @Test
    fun `unanswered or any non completed status is a missed call`() {
        assertFalse(CallDigestPolicy.isMissed(record()))
        assertTrue(CallDigestPolicy.isMissed(record().copy(answered = false)))
        for (status in listOf(CallRecordStatus.NOT_CONNECTED, CallRecordStatus.FAILED, CallRecordStatus.UNKNOWN, CallRecordStatus("NEW_STATUS"))) {
            assertTrue(CallDigestPolicy.isMissed(record().copy(status = status)))
        }
    }

    private fun initialized() = CallDigestPolicy(CallDigestState(watermark = 1))

    private fun record(startedAt: Long = 100) = CallRecordItem(
        callId = "call_test_000000000001", revision = "1", direction = CallDirection.INBOUND,
        address = "138 0000 0000", startedAt = startedAt, endedAt = startedAt + 1,
        durationMs = 1, status = CallRecordStatus.COMPLETED, answered = true,
        source = CallSource.AGENT, summaryState = CallSummaryState.READY,
        summaryPreview = null, hasTranscript = false, triageOutcome = null,
    )
}
