package ai.bondings.callpilot.protocol

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SignalingEventTest {
    @Test
    fun `decodes status, ended, media_failed and hangup ack`() {
        assertEquals(Signaling.Event.Status("connected", null), Signaling.decodeEvent("""{"type":"status","status":"connected","clientTimingVersion":1}"""))
        assertEquals(Signaling.Event.Ended("call_ended"), Signaling.decodeEvent("""{"type":"ended","reason":"call_ended"}"""))
        assertEquals(Signaling.Event.MediaFailed("no_phone_audio"), Signaling.decodeEvent("""{"type":"media_failed","reason":"no_phone_audio"}"""))
        assertEquals(Signaling.Event.HangupAck("x"), Signaling.decodeEvent("""{"type":"hangup.ack","commandId":"x"}"""))
        assertNull(Signaling.decodeEvent("""{"type":"status"}"""))
        assertNull(Signaling.decodeEvent("""{"type":"unknown","status":"x"}"""))
        assertNull(Signaling.decodeEvent("not json"))
    }

    @Test
    fun `malformed field types never throw`() {
        assertNull(Signaling.decodeEvent("""{"type":[]}"""))
        assertNull(Signaling.decodeEvent("""{"type":{"a":1}}"""))
        assertEquals(Signaling.Event.Ended("ended"), Signaling.decodeEvent("""{"type":"ended","reason":{}}"""))
        assertNull(Signaling.decodeEvent("""{"type":"status","status":7}"""))
        assertNull(Signaling.decodeEvent("""[1,2,3]"""))
    }
}
