package ai.bondings.callpilot.diagnostics

import android.app.Application
import android.content.ClipData
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.content.FileProvider
import java.io.File
import java.time.Instant
import java.util.Collections
import java.util.IdentityHashMap
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Four bounded segments; readers, rotation and crash writes share the same lock. */
internal class DiagStore(
    private val directory: File,
    private val segmentBytes: Int = 512 * 1024,
    private val segments: Int = 4,
) {
    init { require(segmentBytes > 1 && segments > 0) }
    private fun segment(index: Int) = File(directory, "flow.$index.log")

    @Synchronized fun append(text: String) {
        check(directory.isDirectory || directory.mkdirs())
        // Bound one record as well as the ring, without splitting a UTF-8 code point.
        val bytes = text.toByteArray(Charsets.UTF_8)
        var end = minOf(bytes.size, segmentBytes - 1)
        if (end < bytes.size) {
            while (end > 0 && (bytes[end].toInt() and 0xc0) == 0x80) end--
        }
        val record = bytes.copyOf(end) + byteArrayOf(10)
        if (segment(0).length() + record.size > segmentBytes) {
            if (segment(segments - 1).exists()) check(segment(segments - 1).delete())
            for (i in segments - 2 downTo 0) {
                if (segment(i).exists()) check(segment(i).renameTo(segment(i + 1)))
            }
        }
        segment(0).appendBytes(record)
    }

    @Synchronized fun snapshot(): String = (segments - 1 downTo 0).joinToString("") {
        segment(it).takeIf(File::exists)?.readText(Charsets.UTF_8).orEmpty()
    }

    @Synchronized fun markCrash() { File(directory, "crashed").writeText("1") }
    @Synchronized fun consumeCrashMarker(): Boolean {
        val marker = File(directory, "crashed")
        return marker.exists().also { if (it) check(marker.delete()) }
    }
}

internal class DiagnosticCrashHandler(
    private val store: DiagStore,
    private val previous: Thread.UncaughtExceptionHandler?,
) : Thread.UncaughtExceptionHandler {
    override fun uncaughtException(thread: Thread, error: Throwable) {
        try {
            val prefix = "${Instant.now()} callId=${DiagLog.activeCallId ?: "-"} stage=crash "
            store.append(DiagLog.stack(error).lineSequence().filter { it.isNotBlank() }
                .joinToString("\n") { prefix + it })
            store.markCrash()
        } catch (_: Throwable) {
            // Never prevent Android's original crash reporting/termination path.
        } finally {
            if (previous != null) previous.uncaughtException(thread, error)
            else { android.os.Process.killProcess(android.os.Process.myPid()); kotlin.system.exitProcess(10) }
        }
    }
}

object DiagLog {
    const val TAG = "CallPilotFlow"
    @Volatile private var store: DiagStore? = null
    @Volatile var activeCallId: String? = null
        private set

    fun bindCall(callId: String?) { activeCallId = callId?.let(::identifier) }

    fun install(context: Context) {
        if (store != null) return
        val target = DiagStore(File(context.noBackupFilesDir, "diagnostics"))
        store = target
        Thread.setDefaultUncaughtExceptionHandler(DiagnosticCrashHandler(target, Thread.getDefaultUncaughtExceptionHandler()))
        runCatching { if (target.consumeCrashMarker()) event("previous_abnormal_exit") }
        event("app_start")
    }

    fun event(stage: String, detail: String = "", error: Throwable? = null, callId: String? = activeCallId) {
        val line = "${Instant.now()} callId=${identifier(callId ?: "-")} stage=$stage ${redact(detail)}" +
            (error?.let { " exception=${it.javaClass.name}" } ?: "")
        // No BuildConfig.DEBUG gate: these events are needed in release too.
        runCatching { Log.i(TAG, line) }
        runCatching { store?.append(line) }
    }

    fun phone(number: String): String = "***" + number.filter(Char::isDigit).takeLast(4)

    internal fun identifier(value: String): String = when {
        value.isNotEmpty() && value.all(Char::isDigit) -> phone(value)
        value.length <= 128 && value.matches(Regex("[A-Za-z0-9_-]+")) -> value
        else -> "[invalid_id]"
    }

    fun code(value: String?): String = when {
        value == null -> "-"
        value.length <= 80 && value.matches(Regex("[a-zA-Z][a-zA-Z0-9_]*")) -> value
        else -> "[non_code_omitted]"
    }

    fun redact(text: String): String = text
        .replace(Regex("(?i)Bearer\\s+[^\\s,;]+"), "Bearer [redacted]")
        .replace(Regex("(?i)([\\\"]?(?:[a-z_]*token|credential|password|secret|authorization)[\\\"]?\\s*[:=]\\s*)[\\\"]?[^\\s,;}\\\"]+[\\\"]?"), "$1[redacted]")
        .replace(Regex("(?<![a-zA-Z0-9])\\+?\\d[\\d ()-]{5,}\\d")) { phone(it.value) }
        .replace('\n', ' ').replace('\r', ' ')

    // Deliberately omit Throwable.message, thread names, suppressed messages and SDK toString().
    internal fun stack(error: Throwable): String = buildString {
        val seen = Collections.newSetFromMap(IdentityHashMap<Throwable, Boolean>())
        fun visit(value: Throwable, prefix: String, depth: Int) {
            if (depth >= 16 || !seen.add(value)) return
            append(prefix).append(value.javaClass.name).append('\n')
            value.stackTrace.take(128).forEach { append("  at ").append(it).append('\n') }
            value.suppressed.take(8).forEach { visit(it, "Suppressed: ", depth + 1) }
            value.cause?.let { visit(it, "Caused by: ", depth + 1) }
        }
        visit(error, "", 0)
    }

    suspend fun export(context: Context): Intent = withContext(Dispatchers.IO) {
        val directory = File(context.cacheDir, "diagnostic-export").apply { mkdirs() }
        val file = File(directory, "callpilot-diagnostics.txt")
        file.writeText(store?.snapshot().orEmpty())
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.diagnostics", file)
        Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_STREAM, uri)
            clipData = ClipData.newRawUri("diagnostics", uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
    }
}

/** Own subclass: two <provider> entries must not share the FileProvider class name (updates uses the base class). */
class DiagnosticsFileProvider : FileProvider()

class CallPilotApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        DiagLog.install(this)
    }
}
