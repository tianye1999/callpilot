package ai.bondings.callpilot.call

import ai.bondings.callpilot.MainActivity
import ai.bondings.callpilot.R
import ai.bondings.callpilot.protocol.CallRecordItem
import ai.bondings.callpilot.protocol.CallSummary
import ai.bondings.callpilot.protocol.CallSummaryState
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.net.Uri

class CallDigestAlerts(private val context: Context) {
    private val notifications = context.getSystemService(NotificationManager::class.java)

    init {
        notifications.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, context.getString(R.string.digest_channel), NotificationManager.IMPORTANCE_HIGH).apply {
                setSound(
                    RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION),
                    AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build(),
                )
                enableVibration(true)
                lockscreenVisibility = Notification.VISIBILITY_PRIVATE
            },
        )
    }

    fun show(record: CallRecordItem, summary: CallSummary?): Boolean {
        if (!notifications.areNotificationsEnabled() ||
            notifications.getNotificationChannel(CHANNEL_ID)?.importance == NotificationManager.IMPORTANCE_NONE
        ) return false
        val missed = CallDigestPolicy.isMissed(record)
        // PENDING 超时只提醒有来电，不展示仍在生成的摘要或预览。
        val readySummary = summary.takeUnless { record.summaryState == CallSummaryState.PENDING }
        val publicTitle = context.getString(if (missed) R.string.digest_missed_title else R.string.digest_title)
        val urgent = readySummary?.urgency?.trim()?.let { it.equals("high", ignoreCase = true) || it == "高" } == true
        val title = if (!missed && urgent) context.getString(R.string.digest_urgent_title) else publicTitle
        val body = if (missed) {
            record.address?.takeIf(String::isNotBlank) ?: context.getString(R.string.digest_unknown_caller)
        } else {
            val text = readySummary?.text?.takeIf(String::isNotBlank)
                ?: record.summaryPreview?.takeIf { record.summaryState != CallSummaryState.PENDING && it.isNotBlank() }
                ?: context.getString(R.string.digest_detail_hint)
            listOfNotNull(readySummary?.callerIdentity?.takeIf(String::isNotBlank), text).joinToString("\n")
        }
        val intent = Intent(context, MainActivity::class.java)
            .setAction(ACTION_CALL_DIGEST)
            .setData(Uri.Builder().scheme("callpilot").authority("records").appendPath(record.callId).build())
            .putExtra(EXTRA_CALL_ID, record.callId)
            .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val pending = PendingIntent.getActivity(
            context, 2, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val publicVersion = Notification.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_callpilot_notification)
            .setContentTitle(publicTitle)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .setContentIntent(pending)
            .setAutoCancel(true)
            .build()
        val notification = Notification.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_callpilot_notification)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setPublicVersion(publicVersion)
            .setContentIntent(pending)
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
            .build()
        return try {
            // 高位划分 ID 范围，tag 再隔离 hash 碰撞，不覆盖转接/前台通知。
            notifications.notify("digest:${record.callId}", notificationId(record.callId), notification)
            true
        } catch (_: SecurityException) {
            false
        }
    }

    companion object {
        const val ACTION_CALL_DIGEST = "ai.bondings.callpilot.CALL_DIGEST"
        const val EXTRA_CALL_ID = "digest_call_id"
        private const val CHANNEL_ID = "callpilot_call_digests"

        internal fun notificationId(callId: String): Int = 0x40000000 or (callId.hashCode() and 0x3fffffff)

        fun clearNotifications(context: Context) {
            val notifications = context.getSystemService(NotificationManager::class.java)
            notifications.activeNotifications.filter { it.notification.channelId == CHANNEL_ID }.forEach {
                notifications.cancel(it.tag, it.id)
            }
        }
    }
}
