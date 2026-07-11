package ai.bondings.callpilot.protocol

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CredentialRedactionTest {
    @Test
    fun `DeviceCredential toString 不包含 secret`() {
        val text = DeviceCredential("device_abcdefghijkl", "secret-plaintext").toString()

        assertTrue(text.contains("device_abcdefghijkl"))
        assertFalse(text.contains("secret-plaintext"))
    }

    @Test
    fun `HostedPairResult toString 不包含 secret`() {
        val result = HostedPairResult(
            device = HostedDevice("device_abcdefghijkl", "edge_abcdefghijkl"),
            credential = DeviceCredential("device_abcdefghijkl", "secret-plaintext"),
        )

        assertTrue(result.toString().contains("device_abcdefghijkl"))
        assertFalse(result.toString().contains("secret-plaintext"))
    }

    @Test
    fun `HostedCallSession toString 不包含 token`() {
        val session = HostedCallSession(
            sessionId = "call_abcdefghijkl",
            livekitUrl = "wss://lk.example.com",
            token = "jwt-plaintext",
            expiresAt = 9999,
        )

        assertTrue(session.toString().contains("call_abcdefghijkl"))
        assertFalse(session.toString().contains("jwt-plaintext"))
    }
}
