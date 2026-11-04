package ai.bondings.callpilot.call

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class OutboundTimingTest {
    @Test fun `fixed fields relative milliseconds and first observation wins`() {
        var now = 100.0
        val timing = OutboundTiming { now }
        now = 112.8
        timing.mark(OutboundTiming.Mark.FACTORY)
        now = 180.0
        timing.mark(OutboundTiming.Mark.FACTORY)
        val line = timing.line()
        assertTrue(line.startsWith("outbound_timing tap=0 "))
        assertTrue(line.contains("factory=12"))
        assertTrue(line.contains("response=-1"))
        assertFalse(line.contains('\n'))
    }

    @Test fun `server timing retains only known numeric durations never descriptions or secrets`() {
        val timing = OutboundTiming { 0.0 }
        timing.serverTiming("auth_checks;dur=2.5;desc=\"secret-token\", presence;dur=4, unknown;dur=13800138000, sdp_relay;desc=\"v=0\\nSDP\", edge_wait;dur=NaN, call_insert;dur=-1")
        assertTrue(timing.line().endsWith("server_timing=auth_checks:2.5,presence:4.0"))
        for (secret in listOf("secret-token", "13800138000", "v=0", "SDP", "NaN", "desc")) {
            assertFalse(secret, timing.line().contains(secret))
        }
        timing.serverTiming(null)
        assertTrue(timing.line().endsWith("server_timing=none"))
        assertEquals(1, timing.line().lines().size)
    }
}
