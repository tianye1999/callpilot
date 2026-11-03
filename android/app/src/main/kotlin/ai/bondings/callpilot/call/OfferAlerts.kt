package ai.bondings.callpilot.call

import ai.bondings.callpilot.MainActivity
import ai.bondings.callpilot.R
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.HostedCloudClient
import ai.bondings.callpilot.protocol.HostedCloudException
import ai.bondings.callpilot.protocol.InboundOffer
import ai.bondings.callpilot.protocol.InboundOfferState
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import androidx.annotation.MainThread
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/** 所有入口在主线程串行更新，避免 Activity 拒绝后被服务的旧响应重新提醒。 */
@MainThread
class OfferAlerts private constructor(private val context: Context) {
    private val notifications = context.getSystemService(NotificationManager::class.java)
    private val prefs = context.getSharedPreferences("offer_alerts", Context.MODE_PRIVATE)
    private var saved = runCatching {
        Json.decodeFromString<Map<String, OfferAlertRecord>>(prefs.getString("handled", "{}")!!)
    }.getOrDefault(emptyMap())
    private var policy = OfferAlertPolicy(saved)
    private var offers = emptyList<InboundOffer>()
    private var foreground = false
    private var busy = false
    private val mutableResolutions = MutableStateFlow<Map<String, InboundOfferState>>(emptyMap())
    private val mutableDismissals = MutableStateFlow<Set<String>>(emptySet())

    /** 本机拒绝（含通知上的按钮）：前台接听界面立即收起，不等下一次列表轮询。 */
    val dismissals: StateFlow<Set<String>> = mutableDismissals.asStateFlow()
    // 拒绝上报不随 Activity 销毁而取消（锁屏上拒绝后界面立刻收起）。
    private val relayScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** 中转长轮询得知的 offer 终态（他机接听 / 机主拒绝 / 结束）；前台据此立即收起接听界面。 */
    val resolutions: StateFlow<Map<String, InboundOfferState>> = mutableResolutions.asStateFlow()

    init {
        notifications.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, context.getString(R.string.offer_channel), NotificationManager.IMPORTANCE_HIGH).apply {
                setSound(
                    RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE),
                    AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build(),
                )
                enableVibration(true)
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            },
        )
        // 进程重建后先清理系统残留通知，已提醒记录仍保留到过期。
        clearNotifications()
    }

    fun bindPairing(deviceId: String) {
        if (prefs.getString("device_id", null) != deviceId) {
            reset()
            prefs.edit().putString("device_id", deviceId).apply()
        }
    }

    fun setForeground(value: Boolean) {
        foreground = value
        // 离开 App 不凭旧快照发新通知，等服务下一次成功查询。
        reconcile(allowNew = false)
    }

    fun update(available: List<InboundOffer>) {
        offers = available
        reconcile(allowNew = true)
    }

    fun tick(isBusy: Boolean) {
        busy = isBusy
        reconcile(allowNew = false)
    }

    fun isDismissed(offerId: String): Boolean = policy.isDismissed(offerId)

    fun presented(offer: InboundOffer) {
        policy.presented(offer)
        persist()
    }

    fun dismiss(offer: InboundOffer) {
        policy.dismiss(offer)
        mutableDismissals.value = (mutableDismissals.value + offer.offerId).toList().takeLast(RESOLUTIONS_KEPT).toSet()
        reconcile(allowNew = false)
    }

    fun reset() {
        clearNotifications()
        policy = OfferAlertPolicy()
        mutableResolutions.value = emptyMap()
        mutableDismissals.value = emptySet()
        persist()
    }

    /** 仍在响铃、需要向中转盯终态的 offer。 */
    fun pendingWatch(now: Long): List<InboundOffer> =
        offers.filter { it.expiresAt > now && !policy.isDismissed(it.offerId) }

    /** 中转告知终态：立刻取消通知、让前台收起，并保证之后的列表快照不再响铃。 */
    fun resolve(offerId: String, state: InboundOfferState) {
        if (policy.resolve(offerId, System.currentTimeMillis())) notifications.cancel(offerId, ALERT_ID)
        offers = offers.filterNot { it.offerId == offerId }
        val updated = mutableResolutions.value + (offerId to state)
        mutableResolutions.value = if (updated.size > RESOLUTIONS_KEPT) {
            updated.entries.drop(updated.size - RESOLUTIONS_KEPT).associate { it.key to it.value }
        } else {
            updated
        }
        reconcile(allowNew = false)
    }

    /**
     * 拒绝即告诉中转：Edge 立刻让 AI 接起，本通不再转回手机。最多重试 3 次；
     * 409（已被接听 / 已结束）与 404（能力未开）是终态，不再重试。
     */
    fun declineOnRelay(pairing: StoredPairing, offerId: String): Job =
        relayScope.launch {
            val client = HostedCloudClient(pairing.gatewayUrl).also { it.credential = pairing.credential }
            repeat(DECLINE_ATTEMPTS) { attempt ->
                try {
                    client.declineInboundOffer(offerId)
                    return@launch
                } catch (error: CancellationException) {
                    throw error
                } catch (error: HostedCloudException) {
                    if (error.statusCode == 409 || error.statusCode == 404 || error.statusCode == 401) return@launch
                } catch (_: Exception) {
                    // 网络抖动：下面重试。
                }
                if (attempt < DECLINE_ATTEMPTS - 1) delay(500L * (attempt + 1))
            }
        }

    fun clearNotifications() {
        offers = emptyList()
        notifications.activeNotifications.filter { it.notification.channelId == CHANNEL_ID }.forEach {
            notifications.cancel(it.tag, it.id)
        }
    }

    private fun reconcile(allowNew: Boolean) {
        val now = System.currentTimeMillis()
        val changes = policy.update(
            offers, now, foreground, busy,
            notificationsAllowed = notifications.areNotificationsEnabled() &&
                notifications.getNotificationChannel(CHANNEL_ID)?.importance != NotificationManager.IMPORTANCE_NONE,
            allowNew = allowNew,
        )
        changes.cancel.forEach { notifications.cancel(it, ALERT_ID) }
        changes.notify.forEach { offer ->
            val intent = Intent(context, MainActivity::class.java)
                .setAction(ACTION_INCOMING_OFFER)
                .setData(Uri.parse("callpilot://inbound/${offer.offerId}"))
                .putExtra(EXTRA_OFFER_ID, offer.offerId)
                .putExtra(EXTRA_EXPIRES_AT, offer.expiresAt)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            val pending = PendingIntent.getActivity(
                context, 1, intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            // 通知上直接「拒绝」：不用打开 App，锁屏、没有全屏通知权限时也能把来电交给助理。
            // 完整 offerId 进 data URI：PendingIntent 按 Intent 身份区分，extras 不参与，
            // 只靠 requestCode（hashCode 会碰撞）会让旧通知的按钮拒绝到新来电。
            val declineIntent = Intent(context, OfferDeclineReceiver::class.java)
                .setAction(OfferDeclineReceiver.ACTION_DECLINE_OFFER)
                .setData(Uri.parse("callpilot://decline/${offer.offerId}"))
                .putExtra(EXTRA_OFFER_ID, offer.offerId)
                .putExtra(EXTRA_EXPIRES_AT, offer.expiresAt)
            val decline = PendingIntent.getBroadcast(
                context, 2, declineIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            val declineAction = Notification.Action.Builder(
                Icon.createWithResource(context, R.drawable.ic_callpilot_notification),
                context.getString(R.string.offer_decline),
                decline,
            ).build()
            fun notification(fullScreen: Boolean): Notification = Notification.Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_callpilot_notification)
                .setContentTitle(context.getString(R.string.offer_notification_title))
                .setContentText(context.getString(R.string.offer_notification_body))
                .setCategory(Notification.CATEGORY_CALL)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setContentIntent(pending)
                .setAutoCancel(true)
                .setOnlyAlertOnce(true)
                .setTimeoutAfter((offer.expiresAt - now).coerceAtLeast(1))
                .addAction(declineAction)
                .apply { if (fullScreen) setFullScreenIntent(pending, true) }
                .build()
            try {
                notifications.notify(offer.offerId, ALERT_ID, notification(canUseFullScreenIntent(context)))
            } catch (_: SecurityException) {
                // 授权可能在检查后被撤销；只退化通知，不从服务直接拉 Activity。
                try {
                    notifications.notify(offer.offerId, ALERT_ID, notification(false))
                } catch (_: SecurityException) {
                    policy.notificationFailed(offer.offerId)
                }
            }
        }
        persist()
    }

    private fun persist() {
        val snapshot = policy.snapshot()
        if (snapshot == saved) return
        saved = snapshot
        prefs.edit().putString("handled", Json.encodeToString(snapshot)).apply()
    }

    companion object {
        const val ACTION_INCOMING_OFFER = "ai.bondings.callpilot.INCOMING_OFFER"
        const val EXTRA_OFFER_ID = "offer_id"
        const val EXTRA_EXPIRES_AT = "offer_expires_at"
        private const val CHANNEL_ID = "callpilot_incoming_offers"
        private const val ALERT_ID = 1003
        private const val RESOLUTIONS_KEPT = 8
        private const val DECLINE_ATTEMPTS = 3
        // 只保存 applicationContext，不持有 Activity / Service。
        @SuppressLint("StaticFieldLeak")
        private var instance: OfferAlerts? = null

        fun get(context: Context): OfferAlerts = instance ?: OfferAlerts(context.applicationContext).also { instance = it }

        fun canUseFullScreenIntent(context: Context): Boolean = Build.VERSION.SDK_INT < 34 ||
            context.getSystemService(NotificationManager::class.java).canUseFullScreenIntent()
    }
}
