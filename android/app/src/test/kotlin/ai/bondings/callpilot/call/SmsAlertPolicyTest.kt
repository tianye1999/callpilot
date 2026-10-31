package ai.bondings.callpilot.call

import ai.bondings.callpilot.protocol.*
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Test

class SmsAlertPolicyTest {
    private val now = 1_000_000L

    @Test
    fun `first run records current time without replaying history`() {
        val policy = SmsAlertPolicy()
        assertTrue(policy.pending(listOf(message()), now).isEmpty())
        assertEquals(now, policy.snapshot().watermark)
        assertTrue(policy.pending(listOf(message(recordedAt = now)), now + 1).isEmpty())
        assertEquals(1, policy.pending(listOf(message(recordedAt = now + 1)), now + 2).size)
    }

    @Test
    fun `only received inbound messages qualify`() {
        for (direction in MessageDirection.entries) {
            for (status in MessageDeliveryStatus.entries) {
                val actions = initialized().pending(listOf(message().copy(direction = direction, status = status)), now)
                assertEquals(direction == MessageDirection.INBOUND && status == MessageDeliveryStatus.RECEIVED, actions.isNotEmpty())
            }
        }
    }

    @Test
    fun `recorded time rather than sender time determines eligibility`() {
        val policy = SmsAlertPolicy(SmsAlertState(watermark = now))
        assertEquals(1, policy.pending(listOf(message(recordedAt = now + 1).copy(occurredAt = 0)), now + 2).size)
        assertTrue(policy.pending(listOf(message(recordedAt = now - 1).copy(occurredAt = now + 1)), now + 2).isEmpty())
    }

    @Test
    fun `new messages alert once and deduplication survives restore`() {
        val policy = initialized()
        val actions = policy.pending(listOf(message(), message()), now)
        assertEquals(listOf(SmsAlertAction(message(), silent = false)), actions)
        policy.notified(message())
        val restored = SmsAlertPolicy(Json.decodeFromString<SmsAlertState>(Json.encodeToString(policy.snapshot())))
        assertTrue(restored.pending(listOf(message()), now + 15_000).isEmpty())
    }

    @Test
    fun `changed revision silently updates and is remembered only after delivery`() {
        val policy = initialized()
        policy.notified(message())
        val updated = message().copy(revision = "revision_test_000000000002", text = "Test message continued")
        val action = SmsAlertAction(updated, silent = true)
        assertEquals(listOf(action), policy.pending(listOf(updated), now))
        assertEquals(listOf(action), policy.pending(listOf(updated), now + 15_000))
        policy.notified(updated)
        assertTrue(SmsAlertPolicy(policy.snapshot()).pending(listOf(updated), now + 30_000).isEmpty())
    }

    @Test
    fun `failed notification remains pending and later polls do not advance watermark`() {
        val policy = initialized()
        assertEquals(1, policy.pending(listOf(message()), now).size)
        policy.pending(emptyList(), now + 15_000)
        assertEquals(1, policy.pending(listOf(message()), now + 30_000).size)
        assertEquals(1L, policy.snapshot().watermark)
    }

    @Test
    fun `history keeps newest 500 IDs and revision changes do not reorder history`() {
        val policy = initialized()
        repeat(500) { policy.notified(message().copy(messageId = "msg_test_${it.toString().padStart(12, '0')}")) }
        val oldest = message().copy(messageId = "msg_test_000000000000", revision = "revision_test_000000000002")
        policy.notified(oldest)
        policy.notified(message().copy(messageId = "msg_test_000000000500"))
        val saved = policy.snapshot()
        assertEquals(500, saved.notifiedMessages.size)
        assertEquals("msg_test_000000000001", saved.notifiedMessages.first().messageId)
        assertEquals("msg_test_000000000500", saved.notifiedMessages.last().messageId)
        val oversized = saved.copy(notifiedMessages = listOf(SmsAlertHistory(oldest.messageId, oldest.revision)) + saved.notifiedMessages)
        assertEquals(saved, SmsAlertPolicy(oversized).snapshot())
    }

    @Test
    fun `state contains no sender or message body`() {
        val policy = initialized()
        policy.notified(message())
        val saved = Json.encodeToString(policy.snapshot())
        assertFalse(saved.contains("10086"))
        assertFalse(saved.contains("Test message"))
        assertTrue(saved.contains(message().messageId))
        assertTrue(saved.contains(message().revision))
    }

    @Test
    fun `latest page is processed oldest first for bounded history`() {
        val older = message(recordedAt = 100)
        val newer = message(recordedAt = 200).copy(messageId = "msg_test_000000000002")
        assertEquals(listOf(older, newer), initialized().pending(listOf(newer, older), now).map { it.message })
    }

    private fun initialized() = SmsAlertPolicy(SmsAlertState(watermark = 1))

    private fun message(recordedAt: Long = 100) = SMSMessage(
        messageId = "msg_test_000000000001", revision = "revision_test_000000000001",
        direction = MessageDirection.INBOUND, address = "10086", text = "Test message",
        occurredAt = recordedAt, recordedAt = recordedAt, status = MessageDeliveryStatus.RECEIVED,
    )
}
