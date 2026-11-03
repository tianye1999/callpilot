package ai.bondings.callpilot.updates

import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Assert.*
import org.junit.Test
import org.w3c.dom.Element
import org.xml.sax.InputSource

class UpdateResourcesTest {
    @Test fun `hangup progress has both locales without AI wording`() {
        assertEquals("Ending call...", strings("values", "strings")["call_hanging_up"])
        assertEquals("正在挂断...", strings("values-zh-rCN", "strings")["call_hanging_up"])
        listOf("values", "values-zh-rCN").forEach {
            assertFalse(strings(it, "strings").getValue("call_hanging_up").contains("AI"))
        }
    }

    private fun xml(path: String) = File(path).reader(Charsets.UTF_8).use {
        DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(InputSource(it))
    }

    private fun strings(locale: String, file: String): Map<String, String> {
        val nodes = xml("src/main/res/$locale/$file.xml").getElementsByTagName("string")
        return (0 until nodes.length).associate {
            val element = nodes.item(it) as Element
            element.getAttribute("name") to element.textContent
        }
    }

    @Test fun `update and offer strings have matching locales and no old visible name`() {
        listOf("app_updates", "offer_watch").forEach { file ->
            val english = strings("values", file)
            val chinese = strings("values-zh-rCN", file)
            assertEquals(english.keys, chinese.keys)
            (english.values + chinese.values).forEach { assertFalse(it.contains("CallPilot")) }
        }
        assertEquals("KinyAI SIM Assistant", strings("values", "strings")["app_name"])
        assertEquals("KinyAI-SIM助手", strings("values-zh-rCN", "strings")["app_name"])
        assertEquals("KinyAI SIM", strings("values", "launcher")["launcher_name"])
        listOf("values", "values-zh-rCN").forEach {
            val offer = strings(it, "offer_watch")
            assertFalse(offer.getValue("offer_notification_body").contains("AI"))
            assertFalse(offer.getValue("offer_decline_hint").contains("AI"))
        }
    }

    @Test fun `installer provider only shares private updates directory`() {
        val manifest = xml("src/main/AndroidManifest.xml")
        val providers = manifest.getElementsByTagName("provider")
        val provider = (0 until providers.length).map { providers.item(it) as Element }
            .single { it.getAttribute("android:authorities") == "\${applicationId}.updates" }
        assertEquals("false", provider.getAttribute("android:exported"))
        assertEquals("true", provider.getAttribute("android:grantUriPermissions"))
        val paths = xml("src/main/res/xml/update_file_paths.xml").documentElement.childNodes
        val elements = (0 until paths.length).mapNotNull { paths.item(it) as? Element }
        assertEquals(1, elements.size)
        assertEquals("external-files-path", elements.single().tagName)
        assertEquals("Download/updates/", elements.single().getAttribute("path"))
    }

    @Test fun `download entry is private and completion receiver requires system signature permission`() {
        val manifest = xml("src/main/AndroidManifest.xml")
        val activities = manifest.getElementsByTagName("activity")
        val installer = (0 until activities.length).map { activities.item(it) as Element }
            .single { it.getAttribute("android:name") == ".updates.UpdateActivity" }
        assertEquals("false", installer.getAttribute("android:exported"))
        val receivers = manifest.getElementsByTagName("receiver")
        val completion = (0 until receivers.length).map { receivers.item(it) as Element }
            .single { it.getAttribute("android:name") == ".updates.UpdateDownloadReceiver" }
        assertEquals("true", completion.getAttribute("android:exported"))
        assertEquals("android.permission.SEND_DOWNLOAD_COMPLETED_INTENTS", completion.getAttribute("android:permission"))
    }
}
