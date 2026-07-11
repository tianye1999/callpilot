package ai.bondings.callpilot.protocol

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PairingLinkTest {

    @Test
    fun `完整配对链接同时解析出网关与配对码`() {
        val p = PairingLink.parse("https://dial.example.com/remote_dialer.html#pair=ab12-cd34")
        assertEquals("https://dial.example.com", p.gatewayBase)
        assertEquals("AB12CD34", p.code)
    }

    @Test
    fun `带端口的网关保留端口`() {
        val p = PairingLink.parse("https://dial.example.com:8443/x.html#pair=AAAA-BBBB")
        assertEquals("https://dial.example.com:8443", p.gatewayBase)
    }

    @Test
    fun `裸配对码 带横线或不带 都接受`() {
        assertEquals("AB12CD34", PairingLink.parse("ab12-cd34").code)
        assertEquals("AB12CD34", PairingLink.parse("AB12CD34").code)
        assertNull(PairingLink.parse("ab12-cd34").gatewayBase)
    }

    @Test
    fun `链接无 pair fragment 时只有网关`() {
        val p = PairingLink.parse("https://dial.example.com/remote_dialer.html")
        assertEquals("https://dial.example.com", p.gatewayBase)
        assertNull(p.code)
    }

    @Test
    fun `垃圾输入返回空`() {
        assertTrue(PairingLink.parse("随便什么").isEmpty)
        assertTrue(PairingLink.parse("").isEmpty)
        assertTrue(PairingLink.parse("ABC-123").isEmpty)
    }

    @Test
    fun `normalize 与 format 往返`() {
        assertEquals("AB12CD34", PairingLink.normalizeCode(" ab12-cd34 ".trim()))
        assertNull(PairingLink.normalizeCode("ABC"))
        assertEquals("AB12-CD34", PairingLink.formatCode("AB12CD34"))
    }
}
