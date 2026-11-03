package ai.bondings.callpilot.ui

import ai.bondings.callpilot.R
import androidx.annotation.StringRes

object CallFailureCopy {
    @StringRes
    fun resource(code: String?): Int = when (code) {
        "EDGE_OFFLINE" -> R.string.call_error_edge_offline
        "MODEM_OFFLINE" -> R.string.call_error_modem_offline
        "SIM_NOT_READY" -> R.string.call_error_sim_not_ready
        "SIM_NOT_REGISTERED" -> R.string.call_error_sim_not_registered
        "SERVICE_NUMBER_MISMATCH" -> R.string.call_error_service_number_mismatch
        "SERVICE_NUMBER_UNKNOWN" -> R.string.call_error_service_number_unknown
        "LINE_BUSY" -> R.string.call_error_line_busy
        "RATE_LIMITED" -> R.string.call_error_rate_limited
        "TAKEOVER_MEDIA_TIMEOUT", "SESSION_TIMEOUT", "EDGE_TIMEOUT", "TIMEOUT" -> R.string.call_error_timeout
        "OFFER_UNAVAILABLE" -> R.string.call_error_offer_unavailable
        else -> R.string.call_error_generic
    }

    fun format(code: String?, localized: String): String =
        if (resource(code) == R.string.call_error_generic && !code.isNullOrBlank()) {
            "$localized ($code)"
        } else localized
}
