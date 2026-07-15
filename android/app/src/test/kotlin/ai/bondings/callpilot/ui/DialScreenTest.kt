package ai.bondings.callpilot.ui

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DialScreenTest {

    @Test
    fun `拨号按钮同时要求号码合法和线路就绪`() {
        assertTrue(isDialEnabled("10086", lineReady = true))
        assertFalse(isDialEnabled("10086", lineReady = false))
        assertFalse(isDialEnabled("10086", lineReady = null))
        assertFalse(isDialEnabled("not-a-number", lineReady = true))
    }
}
