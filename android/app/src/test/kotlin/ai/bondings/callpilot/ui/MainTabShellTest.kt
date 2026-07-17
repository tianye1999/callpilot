package ai.bondings.callpilot.ui

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
    }

    @Test
    fun `detail destination keeps its parent tab selected`() {
        assertEquals(true, isMainTabSelected(MainTab.Messages, "messages/detail/{messageId}"))
        assertEquals(false, isMainTabSelected(MainTab.Records, "messages/detail/{messageId}"))
        assertEquals(true, isMainTabSelected(MainTab.Records, "records/detail/{callId}"))
        assertEquals(false, isMainTabSelected(MainTab.Messages, "records/detail/{callId}"))
    }
}
