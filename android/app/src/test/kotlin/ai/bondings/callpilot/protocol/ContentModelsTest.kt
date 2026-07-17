package ai.bondings.callpilot.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class ContentModelsTest {
    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `shared messages fixture decodes without merging carrier fragments`() {
        val page = MessagePage.decode(json, ContentTestFixtures.text("messages-page.json"))

        assertEquals(1, page.v)
        assertEquals(3, page.items.size)
        assertEquals(MessageDirection.OUTBOUND, page.items.first().direction)
        assertEquals(MessageDeliveryStatus.SENT, page.items.first().status)
        assertEquals("cursor_messages_fixture_0001", page.nextCursor)
        val fragments = page.items.filter { it.occurredAt == 1_784_160_200_000 }
        assertEquals(2, fragments.size)
        assertNotEquals(fragments[0].messageId, fragments[1].messageId)
    }

    @Test
    fun `unknown fields are ignored but identity and cursor invariants fail closed`() {
        val source = json.parseToJsonElement(ContentTestFixtures.text("messages-page.json")).jsonObject
        val unknown = source.toMutableMap().also { it["futureField"] = json.parseToJsonElement("true") }
        assertEquals(3, MessagePage.decode(json, json.encodeToString(unknown)).items.size)

        val invalidCursor = ContentTestFixtures.text("messages-page.json")
            .replace("\"nextCursor\": \"cursor_messages_fixture_0001\"", "\"nextCursor\": null")
        assertThrows(ContentContractException::class.java) {
            MessagePage.decode(json, invalidCursor)
        }

        val invalidId = ContentTestFixtures.text("messages-page.json")
            .replace("msg_fixture_outbound_0001", "local-file-name")
        assertThrows(ContentContractException::class.java) {
            MessagePage.decode(json, invalidId)
        }
    }

    @Test
    fun `boolean timestamp and outbound received status are rejected`() {
        val booleanTime = ContentTestFixtures.text("messages-page.json")
            .replace("\"occurredAt\": 1784160300000", "\"occurredAt\": true")
        assertThrows(ContentContractException::class.java) {
            MessagePage.decode(json, booleanTime)
        }

        val invalidStatus = ContentTestFixtures.text("messages-page.json")
            .replaceFirst("\"status\": \"SENT\"", "\"status\": \"RECEIVED\"")
        assertThrows(ContentContractException::class.java) {
            MessagePage.decode(json, invalidStatus)
        }
    }
}
