package ai.bondings.callpilot.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

class SignalingTest {

    private fun fields(raw: String): Map<String, String> =
        Json.parseToJsonElement(raw).jsonObject.mapValues { it.value.jsonPrimitive.content }

    @Test
    fun `dial 命令 schema 与 Edge 对齐`() {
        val raw = Signaling.encodeDial("10000", "idem-1")
        val obj = fields(raw)
        assertEquals("dial", obj["type"])
        assertEquals("10000", obj["number"])
        assertEquals("idem-1", obj["idempotency_key"])
    }

    @Test
    fun `非法号码在客户端就拒绝`() {
        assertThrows(IllegalArgumentException::class.java) {
            Signaling.encodeDial("123abc", "idem")
        }
        assertThrows(IllegalArgumentException::class.java) {
            Signaling.encodeDial("", "idem")
        }
        // 33 位超长
        assertThrows(IllegalArgumentException::class.java) {
            Signaling.encodeDial("1".repeat(33), "idem")
        }
    }

    @Test
    fun `dtmf 校验 0-9星井 1-16 位`() {
        val obj = fields(Signaling.encodeDtmf("1*#0"))
        assertEquals("dtmf", obj["type"])
        assertEquals("1*#0", obj["digits"])
        assertThrows(IllegalArgumentException::class.java) { Signaling.encodeDtmf("") }
        assertThrows(IllegalArgumentException::class.java) { Signaling.encodeDtmf("1".repeat(17)) }
        assertThrows(IllegalArgumentException::class.java) { Signaling.encodeDtmf("abc") }
    }

    @Test
    fun `hangup 命令`() {
        assertEquals("hangup", fields(Signaling.encodeHangup())["type"])
    }

    @Test
    fun `解析 status 与 remote_call 事件`() {
        assertEquals(
            Signaling.Event.Status("media_ready"),
            Signaling.decodeEvent("""{"type":"status","status":"media_ready"}"""),
        )
        assertEquals(
            Signaling.Event.RemoteCall("connected"),
            Signaling.decodeEvent("""{"type":"remote_call","status":"connected"}"""),
        )
    }

    @Test
    fun `未知类型与坏 JSON 返回 null 而不是崩溃`() {
        assertNull(Signaling.decodeEvent("""{"type":"future_thing","status":"x"}"""))
        assertNull(Signaling.decodeEvent("""{"type":"status"}"""))
        assertNull(Signaling.decodeEvent("not json"))
        assertNull(Signaling.decodeEvent("[]"))
    }
}
