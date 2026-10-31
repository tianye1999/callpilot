package ai.bondings.callpilot.call

import ai.bondings.callpilot.MainActivity
import ai.bondings.callpilot.R
import ai.bondings.callpilot.protocol.SMSMessage
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.net.Uri
import androidx.core.app.NotificationCompat

class SmsAlerts(private val context: Context) {
    private val notifications = context.getSystemService(NotificationManager::class.java)

    init {
        notifications.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, context.getString(R.string.sms_alert_channel), NotificationManager.IMPORTANCE_HIGH).apply {
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

    fun show(message: SMSMessage, silent: Boolean): Boolean {
        if (!notifications.areNotificationsEnabled() ||
            notifications.getNotificationChannel(CHANNEL_ID)?.importance == NotificationManager.IMPORTANCE_NONE
        ) return false
        val intent = Intent(context, MainActivity::class.java)
            .setAction(ACTION_SMS)
            .setData(Uri.Builder().scheme("callpilot").authority("messages").appendPath(message.messageId).build())
            .putExtra(EXTRA_MESSAGE_ID, message.messageId)
            .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val pending = PendingIntent.getActivity(
            context, 3, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val publicVersion = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_callpilot_notification)
            .setContentTitle(context.getString(R.string.sms_alert_public_title))
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setContentIntent(pending)
            .setAutoCancel(true)
            .build()
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_callpilot_notification)
            .setContentTitle(message.address)
            .setContentText(message.text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message.text))
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setPublicVersion(publicVersion)
            .setContentIntent(pending)
            .setAutoCancel(true)
            .setGroup(GROUP_KEY)
            .setOnlyAlertOnce(true)
            // 即使用户已划掉原通知，后续分段更新也不得再次响铃或震动。
            .setSilent(silent)
            .build()
        return try {
            notifications.notify("sms:${message.messageId}", notificationId(message.messageId), notification)
            true
        } catch (_: SecurityException) {
            false
        }
    }

    companion object {
        const val ACTION_SMS = "ai.bondings.callpilot.SMS"
        const val EXTRA_MESSAGE_ID = "sms_message_id"
        private const val CHANNEL_ID = "callpilot_sms"
        private const val GROUP_KEY = "callpilot_sms"

        // 与通话摘要的高位区间分开，tag 再隔离不同 messageId 的 hash 碰撞。
        internal fun notificationId(messageId: String): Int = 0x20000000 or (messageId.hashCode() and 0x1fffffff)

        fun clearNotifications(context: Context) {
            val notifications = context.getSystemService(NotificationManager::class.java)
            notifications.activeNotifications.filter { it.notification.channelId == CHANNEL_ID }.forEach {
                notifications.cancel(it.tag, it.id)
            }
        }
    }
}
