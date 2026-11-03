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
}
