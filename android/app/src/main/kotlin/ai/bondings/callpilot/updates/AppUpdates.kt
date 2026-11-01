package ai.bondings.callpilot.updates

import ai.bondings.callpilot.BuildConfig
import ai.bondings.callpilot.R
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.SystemClock
import android.util.Log
import androidx.annotation.MainThread
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.encodeToString

/** Main-thread singleton: manual and automatic checks cannot race notification claims. */
@MainThread
class AppUpdates private constructor(private val context: Context) {
    private val prefs = context.getSharedPreferences("app_updates", Context.MODE_PRIVATE)
    private val policy = UpdatePolicy(prefs.getStringSet("notified_versions", emptySet()).orEmpty())
    private val client = UpdateClient()
    private val mutex = Mutex()
    private val mutableLatest = MutableStateFlow(savedRelease())
    val latest = mutableLatest.asStateFlow()
    private val mutableChecking = MutableStateFlow(false)
    val checking = mutableChecking.asStateFlow()

    private fun savedRelease(): AppRelease? = runCatching {
        prefs.getString("latest", null)?.let(UpdateClient::parse)
            ?.takeIf { UpdatePolicy.isNewer(it.version, BuildConfig.VERSION_NAME) }
    }.getOrNull()

    suspend fun check(manual: Boolean = false) = mutex.withLock {
        if (!policy.beginCheck(SystemClock.elapsedRealtime(), manual)) return@withLock
        mutableChecking.value = true
        try {
            val release = withContext(Dispatchers.IO) { client.fetch() }
                .takeIf { UpdatePolicy.isNewer(it.version, BuildConfig.VERSION_NAME) }
            mutableLatest.value = release
            prefs.edit().apply {
                if (release == null) remove("latest") else putString("latest", UpdateClient.json.encodeToString(release))
            }.apply()
            if (release != null && policy.shouldNotify(release.version) &&
                notify(context.getString(R.string.update_available, release.version), release.notes.zh)
            ) {
                policy.notified(release.version)
                prefs.edit().putStringSet("notified_versions", policy.snapshot()).apply()
            }
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            // Do not log response bodies, signed download URLs or credentials.
            Log.w(TAG, "Update check failed: ${error.javaClass.simpleName}")
        } finally {
            mutableChecking.value = false
        }
    }

    fun openDownload(context: Context) {
        try {
            context.startActivity(Intent(context, UpdateActivity::class.java))
        } catch (error: Exception) {
            Log.w(TAG, "Cannot open update: ${error.javaClass.simpleName}")
        }
    }

    fun nextCheckDelay(): Long = policy.nextCheckDelay(SystemClock.elapsedRealtime())

    @SuppressLint("MissingPermission")
    internal fun notify(title: String, body: String): Boolean {
        try {
            val manager = context.getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, context.getString(R.string.update_channel), NotificationManager.IMPORTANCE_LOW).apply {
                    setSound(null, null)
                    enableVibration(false)
                },
            )
            if (!manager.areNotificationsEnabled() ||
                manager.getNotificationChannel(CHANNEL_ID)?.importance == NotificationManager.IMPORTANCE_NONE
            ) return false
            val intent = Intent(context, UpdateActivity::class.java)
            val pending = PendingIntent.getActivity(
                context, NOTIFICATION_ID, intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            manager.notify(
                NOTIFICATION_ID,
                Notification.Builder(context, CHANNEL_ID)
                    .setSmallIcon(R.drawable.ic_callpilot_notification)
                    .setContentTitle(title)
                    .setContentText(body)
                    .setStyle(Notification.BigTextStyle().bigText(body))
                    .setContentIntent(pending)
                    .setOnlyAlertOnce(true)
                    .setAutoCancel(true)
                    .build(),
            )
            return true
        } catch (error: Exception) {
            Log.w(TAG, "Update notification failed: ${error.javaClass.simpleName}")
            return false
        }
    }

    internal fun clearNotification() {
        context.getSystemService(NotificationManager::class.java).cancel(NOTIFICATION_ID)
    }

    companion object {
        internal const val TAG = "AppUpdates"
        private const val CHANNEL_ID = "app_updates"
        private const val NOTIFICATION_ID = 1006
        @SuppressLint("StaticFieldLeak")
        private var instance: AppUpdates? = null

        fun get(context: Context): AppUpdates = instance ?: AppUpdates(context.applicationContext).also { instance = it }
    }
}
