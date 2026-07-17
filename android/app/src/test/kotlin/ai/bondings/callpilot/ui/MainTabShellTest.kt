package ai.bondings.callpilot.ui

import ai.bondings.callpilot.content.SettingsDeviceStatus
import ai.bondings.callpilot.protocol.DeviceStatus
import ai.bondings.callpilot.protocol.HostedDeviceStatus
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Test

class MainTabShellTest {
    @Test
    fun `main tabs have stable identity and order`() {
        assertEquals(
            listOf(MainTab.Dial, MainTab.Records, MainTab.Messages, MainTab.Settings),
            MainTab.entries,
        )
        assertEquals(
            listOf("dial", "records", "messages", "settings"),
            MainTab.entries.map(MainTab::route),
        )
        assertEquals(4, MainTab.entries.map(MainTab::labelRes).distinct().size)
    }

    @Test
    fun `detail destination keeps its parent tab selected`() {
        assertEquals(true, isMainTabSelected(MainTab.Messages, "messages/detail/{messageId}"))
        assertEquals(false, isMainTabSelected(MainTab.Records, "messages/detail/{messageId}"))
        assertEquals(true, isMainTabSelected(MainTab.Records, "records/detail/{callId}"))
        assertEquals(false, isMainTabSelected(MainTab.Messages, "records/detail/{callId}"))
    }

    @Test
    fun `successful tunnel status means computer is online even when remote dialing is disabled`() {
        val status = DeviceStatus(
            paired = true,
            device = null,
            edge = JsonObject(
                mapOf(
                    "enabled" to JsonPrimitive(false),
                    "modem_online" to JsonPrimitive(true),
                ),
            ),
        )

        assertEquals(
            SettingsDeviceStatus(edgeOnline = true, modemOnline = true),
            status.toSettingsDeviceStatus(),
        )
    }

    @Test
    fun `hosted status uses cloud connected and modem flags`() {
        val status = HostedDeviceStatus(
            paired = true,
            device = null,
            edge = JsonObject(
                mapOf(
                    "connected" to JsonPrimitive(false),
                    "modemOnline" to JsonPrimitive(true),
                ),
            ),
        )

        assertEquals(
            SettingsDeviceStatus(edgeOnline = false, modemOnline = true),
            status.toSettingsDeviceStatus(),
        )
    }
}
