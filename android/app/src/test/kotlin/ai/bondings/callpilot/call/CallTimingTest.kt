package ai.bondings.callpilot.call

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.double
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CallTimingTest {
    @Test
    fun `里程碑相对起点 每个只发一次 seq 递增`() {
        var now = 1_000.0
        val timing = CallTiming { now }
        now = 900.0
        timing.mark(CallTiming.Mark.PREPARE_START)
        now = 1_200.0
        timing.restartAt(CallTiming.Mark.ANSWER_ACTION)
        now = 1_450.5
        timing.mark(CallTiming.Mark.CLAIM_RESPONSE)
        timing.mark(CallTiming.Mark.CLAIM_RESPONSE)

        val first = Json.parseToJsonElement(timing.nextBatch()!!.text).jsonObject
        assertEquals("client_timing", first["type"]!!.jsonPrimitive.content)
        assertEquals(1, first["seq"]!!.jsonPrimitive.int)
        assertEquals(timing.attempt, first["attempt"]!!.jsonPrimitive.content)
        val marks = first["marks"]!!.jsonObject
        assertEquals(-300.0, marks["prepare_start"]!!.jsonPrimitive.double, 0.001)
        assertEquals(0.0, marks["answer_action"]!!.jsonPrimitive.double, 0.001)
        assertEquals(250.5, marks["claim_response"]!!.jsonPrimitive.double, 0.001)
        assertNull(timing.nextBatch())

        now = 2_000.0
        timing.mark(CallTiming.Mark.EDGE_CONNECTED)
        val second = Json.parseToJsonElement(timing.nextBatch()!!.text).jsonObject
        assertEquals(2, second["seq"]!!.jsonPrimitive.int)
        assertEquals(setOf("edge_connected"), second["marks"]!!.jsonObject.keys)
    }

    @Test
    fun `每批不超过 16 个且未送达的会重发`() {
        val timing = CallTiming { 0.0 }
        CallTiming.Mark.entries.forEach { timing.mark(it) }
        val first = timing.nextBatch()!!
        assertEquals(16, Json.parseToJsonElement(first.text).jsonObject["marks"]!!.jsonObject.size)
        assertTrue(first.text.toByteArray().size <= CallTiming.MAX_BATCH_BYTES)
        timing.unsend(first)
        val retry = Json.parseToJsonElement(timing.nextBatch()!!.text).jsonObject
        assertEquals(2, retry["seq"]!!.jsonPrimitive.int)
        assertEquals(16, retry["marks"]!!.jsonObject.size)
    }

    @Test
    fun `HTTP 补交重新打包全部里程碑 seq 继续递增且合计不超 4 KiB`() {
        val timing = CallTiming { 0.0 }
        CallTiming.Mark.entries.forEach { timing.mark(it) }
        timing.nextBatch()
        val batches = timing.httpBatches()
        assertTrue(batches.size in 1..CallTiming.MAX_HTTP_BATCHES)
        assertTrue(batches.sumOf { it.toByteArray().size + 1 } + 2 <= CallTiming.MAX_HTTP_BYTES)
        val parsed = batches.map { Json.parseToJsonElement(it).jsonObject }
        assertEquals(listOf(2, 3), parsed.map { it["seq"]!!.jsonPrimitive.int })
        assertEquals(CallTiming.Mark.entries.size, parsed.sumOf { it["marks"]!!.jsonObject.size })
    }

    @Test
    fun `HTTP 快照不占用控制通道的待发里程碑 且控制通道用尽额度后仍留给 HTTP`() {
        val timing = CallTiming { 0.0 }
        timing.mark(CallTiming.Mark.CLAIM_START)
        timing.nextBatch()
        timing.mark(CallTiming.Mark.HANGUP_INTENT)
        val snapshot = timing.httpBatches()
        assertTrue(snapshot.single().contains("hangup_intent"))
        // 控制通道仍会发出 hangup_intent（HTTP 可能失败或被更大的 seq 越过）。
        val control = Json.parseToJsonElement(timing.nextBatch()!!.text).jsonObject
        assertEquals(setOf("hangup_intent"), control["marks"]!!.jsonObject.keys)

        val exhausted = CallTiming { 0.0 }
        var seq = 0
        while (true) {
            exhausted.mark(CallTiming.Mark.CONNECT_START)
            val batch = exhausted.nextBatch() ?: break
            exhausted.unsend(batch)
            seq += 1
        }
        assertEquals(CallTiming.MAX_BATCHES - CallTiming.MAX_HTTP_BATCHES, seq)
        assertTrue(exhausted.httpBatches().isNotEmpty())
    }
}
