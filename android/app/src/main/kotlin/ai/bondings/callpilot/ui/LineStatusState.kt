package ai.bondings.callpilot.ui

import java.io.InterruptedIOException
import java.net.SocketTimeoutException

/** Display state and failure budget travel together across page/lifecycle changes. */
internal data class LineStatusState(
    val ready: Boolean? = null,
    val label: String = "线路状态获取中…",
    val error: String? = null,
    val consecutiveFailures: Int = 0,
) {
    val failureTolerated: Boolean
        get() = ready != null && consecutiveFailures == 1 && error == null

    fun onSuccess(ready: Boolean, label: String): LineStatusState =
        LineStatusState(ready = ready, label = label)

    fun onFailure(cause: Exception): LineStatusState {
        val failures = (consecutiveFailures + 1).coerceAtMost(2)
        // A non-null ready value with no prior failures came from a successful poll.
        if (ready != null && failures < 2) return copy(consecutiveFailures = failures)
        val timeout = cause is SocketTimeoutException ||
            (cause is InterruptedIOException && cause.message?.contains("timeout", ignoreCase = true) == true)
        return copy(
            ready = false,
            label = "线路状态暂不可用",
            error = if (timeout) "网络较慢，正在重试" else "获取线路状态失败，请稍后重试",
            consecutiveFailures = failures,
        )
    }
}
