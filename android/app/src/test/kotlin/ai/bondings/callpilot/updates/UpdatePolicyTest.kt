package ai.bondings.callpilot.updates

import org.junit.Assert.*
import org.junit.Test

class UpdatePolicyTest {
    @Test fun `versions compare numeric segments and pad trailing zeroes`() {
        assertTrue(UpdatePolicy.isNewer("0.7.10", "0.7.9"))
        assertTrue(UpdatePolicy.isNewer("1.0", "0.99.99"))
        assertTrue(UpdatePolicy.isNewer("0.7.5.1", "0.7.5"))
        assertFalse(UpdatePolicy.isNewer("0.7.5.0", "0.7.5"))
        assertFalse(UpdatePolicy.isNewer("00.07.005", "0.7.5"))
        assertFalse(UpdatePolicy.isNewer("0.7.4", "0.7.5"))
    }

    @Test fun `invalid versions are rejected on either side`() {
        listOf("", "1", "v0.7.6", "0.7.6-beta", " 0.7.6", "0.7.6\n", "0..8", "-1.0",
            "1.1000000000", "1.2.3.4.5.6.7.8.9", "１.２").forEach {
            assertFalse(it, UpdatePolicy.isNewer(it, "0.7.5"))
            assertFalse(it, UpdatePolicy.isNewer("9.0", it))
        }
    }

    @Test fun `only HTTPS download host is accepted`() {
        assertTrue(UpdatePolicy.validDownloadUrl("https://sim-dl.bondings.ai/android/app.apk?version=0.7.6"))
        assertTrue(UpdatePolicy.validDownloadUrl("https://SIM-DL.BONDINGS.AI/app.apk"))
        listOf("http://sim-dl.bondings.ai/app.apk", "https://evil.example/app.apk",
            "https://sim-dl.bondings.ai.evil.example/app.apk", "https://evil.example@sim-dl.bondings.ai/a",
            "https://sim-dl.bondings.ai@evil.example/a", "https://sim-dl.bondings.ai:0/a",
            "https://sim-dl.bondings.ai:65536/a", "https://sim-dl.bondings.ai\\@evil.example/a",
            "https://sim-dl.bondings.ai/a\n", "file:///app.apk", "not a URL").forEach {
            assertFalse(it, UpdatePolicy.validDownloadUrl(it))
        }
    }

    @Test fun `notifications are remembered across restarts and release rollbacks`() {
        val policy = UpdatePolicy()
        assertTrue(policy.shouldNotify("0.7.6"))
        policy.notified("0.7.6")
        assertFalse(policy.shouldNotify("0.7.6"))
        assertFalse(policy.shouldNotify("0.7.6.0"))
        assertTrue(policy.shouldNotify("0.7.7"))
        policy.notified("0.7.7")
        val restored = UpdatePolicy(policy.snapshot())
        assertFalse(restored.shouldNotify("0.7.6"))
        assertFalse(restored.shouldNotify("0.7.7"))
        assertTrue(restored.shouldNotify("0.7.8"))
        assertFalse(restored.shouldNotify("invalid"))
    }

    @Test fun `automatic checks share six hour throttle including failed attempts`() {
        val policy = UpdatePolicy()
        assertTrue(policy.beginCheck(0))
        assertFalse(policy.beginCheck(0))
        assertFalse(policy.beginCheck(UpdatePolicy.INTERVAL_MS - 1))
        assertTrue(policy.beginCheck(UpdatePolicy.INTERVAL_MS))
        assertFalse(policy.beginCheck(UpdatePolicy.INTERVAL_MS + 1))
    }

    @Test fun `manual check bypasses throttle and resets automatic deadline`() {
        val policy = UpdatePolicy()
        assertTrue(policy.beginCheck(10))
        assertTrue(policy.beginCheck(11, manual = true))
        assertFalse(policy.beginCheck(10 + UpdatePolicy.INTERVAL_MS))
        assertEquals(1L, policy.nextCheckDelay(10 + UpdatePolicy.INTERVAL_MS))
        assertTrue(policy.beginCheck(11 + UpdatePolicy.INTERVAL_MS))
        assertEquals(UpdatePolicy.INTERVAL_MS, policy.nextCheckDelay(11 + UpdatePolicy.INTERVAL_MS))
        assertTrue(UpdatePolicy().beginCheck(1))
    }
}
