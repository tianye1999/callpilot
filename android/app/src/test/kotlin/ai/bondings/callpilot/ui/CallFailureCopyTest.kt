package ai.bondings.callpilot.ui

import ai.bondings.callpilot.R
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Assert.*
import org.junit.Test

class CallFailureCopyTest {
    @Test
    fun `known codes have localized copy in both languages`() {
        val codes = mapOf(
            "EDGE_OFFLINE" to R.string.call_error_edge_offline,
            "MODEM_OFFLINE" to R.string.call_error_modem_offline,
            "SIM_NOT_READY" to R.string.call_error_sim_not_ready,
            "SIM_NOT_REGISTERED" to R.string.call_error_sim_not_registered,
            "SERVICE_NUMBER_MISMATCH" to R.string.call_error_service_number_mismatch,
            "SERVICE_NUMBER_UNKNOWN" to R.string.call_error_service_number_unknown,
            "LINE_BUSY" to R.string.call_error_line_busy,
            "RATE_LIMITED" to R.string.call_error_rate_limited,
            "TAKEOVER_MEDIA_TIMEOUT" to R.string.call_error_timeout,
            "SESSION_TIMEOUT" to R.string.call_error_timeout,
            "EDGE_TIMEOUT" to R.string.call_error_timeout,
            "TIMEOUT" to R.string.call_error_timeout,
            "OFFER_UNAVAILABLE" to R.string.call_error_offer_unavailable,
        )
        val names = listOf("edge_offline", "modem_offline", "sim_not_ready", "sim_not_registered",
            "service_number_mismatch", "service_number_unknown", "line_busy", "rate_limited",
            "timeout", "offer_unavailable", "generic")
        for ((code, resource) in codes) {
            assertEquals(code, resource, CallFailureCopy.resource(code))
            assertEquals("localized", CallFailureCopy.format(code, "localized"))
        }
        for (directory in listOf("values", "values-zh-rCN")) {
            val document = DocumentBuilderFactory.newInstance().newDocumentBuilder()
                .parse(File("src/main/res/$directory/strings.xml"))
            val nodes = document.getElementsByTagName("string")
            val values = (0 until nodes.length).associate {
                nodes.item(it).attributes.getNamedItem("name").nodeValue to nodes.item(it).textContent
            }
            names.forEach { assertFalse("$directory/$it", values["call_error_$it"].isNullOrBlank()) }
        }
    }

    @Test
    fun `unknown code retains generic copy and diagnostic code`() {
        assertEquals(R.string.call_error_generic, CallFailureCopy.resource("FUTURE_ERROR"))
        assertEquals("Try again (FUTURE_ERROR)", CallFailureCopy.format("FUTURE_ERROR", "Try again"))
        assertEquals("Try again", CallFailureCopy.format(null, "Try again"))
        assertEquals("Try again", CallFailureCopy.format("", "Try again"))
    }
}
