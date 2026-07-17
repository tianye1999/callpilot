package ai.bondings.callpilot.ui

import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class M7LocalizationTest {
    @Test
    fun `English and Simplified Chinese resources expose the same keys`() {
        val english = resourceKeys(File("src/main/res/values/strings.xml"))
        val chinese = resourceKeys(File("src/main/res/values-zh-rCN/strings.xml"))

        assertTrue(english.isNotEmpty())
        assertEquals(english, chinese)
    }

    @Test
    fun `M7 shell content and settings screens contain no hardcoded Chinese copy`() {
        val uiSourceFiles = listOf(
            "MainTabShell.kt",
            "MessagesScreen.kt",
            "CallRecordsScreen.kt",
            "SettingsScreen.kt",
        ).map { File("src/main/kotlin/ai/bondings/callpilot/ui/$it") }
        val contentSourceFiles = listOf(
            "MessageInboxModel.kt",
            "CallHistoryModel.kt",
        ).map { File("src/main/kotlin/ai/bondings/callpilot/content/$it") }

        (uiSourceFiles + contentSourceFiles).forEach { source ->
            assertTrue("missing source ${source.path}", source.isFile)
            val hardcodedChinese = source.useLines { lines ->
                lines.any { line -> Regex("\\\"[^\\\"]*[\\u4e00-\\u9fff][^\\\"]*\\\"").containsMatchIn(line) }
            }
            assertFalse(
                "hardcoded Chinese copy remains in ${source.name}",
                hardcodedChinese,
            )
        }
    }

    private fun resourceKeys(file: File): Set<String> {
        assertTrue("missing resource ${file.path}", file.isFile)
        val document = DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(file)
        val strings = document.getElementsByTagName("string")
        return buildSet {
            repeat(strings.length) { index ->
                add(strings.item(index).attributes.getNamedItem("name").nodeValue)
            }
        }
    }
}
