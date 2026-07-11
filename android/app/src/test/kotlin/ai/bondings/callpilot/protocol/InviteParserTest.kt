package ai.bondings.callpilot.protocol

import java.util.Base64
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class InviteParserTest {

    private fun encodeFragment(json: String): String =
        Base64.getUrlEncoder().withoutPadding().encodeToString(json.toByteArray(Charsets.UTF_8))

    @Test
    fun `合法邀请 fragment 解析出 LiveKit 连接信息`() {
        val fragment = encodeFragment(
            """{"v":1,"url":"wss://demo.livekit.cloud","token":"jwt-abc","sessionId":"s-1"}"""
        )
        val payload = InviteParser.parseInvitePayload(fragment)!!
        assertEquals("wss://demo.livekit.cloud", payload.url)
        assertEquals("jwt-abc", payload.token)
        assertEquals("s-1", payload.sessionId)
    }

    @Test
    fun `完整邀请 URL 直接解析`() {
        val fragment = encodeFragment("""{"v":1,"url":"wss://x","token":"t","sessionId":"s"}""")
        val payload = InviteParser.parseInviteUrl("https://dial.example.com/remote_dialer.html#$fragment")!!
        assertEquals("s", payload.sessionId)
    }

    @Test
    fun `协议版本不是 1 时拒绝`() {
        val fragment = encodeFragment("""{"v":2,"url":"wss://x","token":"t","sessionId":"s"}""")
        assertNull(InviteParser.parseInvitePayload(fragment))
    }

    @Test
    fun `非法 base64 与空 fragment 返回 null`() {
        assertNull(InviteParser.parseInvitePayload("!!!not-base64!!!"))
        assertNull(InviteParser.parseInvitePayload(""))
    }

    @Test
    fun `pair 深链解析出配对码且不当作邀请`() {
        assertEquals("AB12-CD34", InviteParser.parsePairingCode("pair=ab12-cd34"))
        assertNull(InviteParser.parseInvitePayload("pair=AB12-CD34"))
        assertNull(InviteParser.parsePairingCode("pair="))
        assertNull(InviteParser.parsePairingCode("其他"))
    }

    @Test
    fun `从 URL 提取 fragment`() {
        assertEquals("abc", InviteParser.fragmentOf("https://x/y#abc"))
        assertEquals("", InviteParser.fragmentOf("https://x/y"))
    }
}
