package ai.bondings.callpilot.call

import ai.bondings.callpilot.pairing.CredentialStore
import ai.bondings.callpilot.protocol.InboundOffer
import ai.bondings.callpilot.protocol.PairingProtocol
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * 来电通知上的「拒绝」按钮：不用打开 App（锁屏、没有全屏通知权限时也能拒）。
 * 本机收起这条来电，并告诉中转「机主拒绝」→ Edge 立即让助理接起，本通不再转回手机。
 */
class OfferDeclineReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val offer = declinedOffer(intent, System.currentTimeMillis()) ?: return
        val pairing = runCatching { CredentialStore(context.applicationContext).load() }.getOrNull()
        val alerts = OfferAlerts.get(context)
        alerts.dismiss(offer)
        if (pairing?.protocol != PairingProtocol.HOSTED) return
        // 进程可能在 onReceive 返回后被回收：goAsync 让拒绝请求有机会发完（系统上限约 10 s）。
        val pending = goAsync()
        alerts.declineOnRelay(pairing, offer.offerId).invokeOnCompletion { pending.finish() }
    }

    companion object {
        const val ACTION_DECLINE_OFFER = "ai.bondings.callpilot.DECLINE_OFFER"
        private val OFFER_ID = Regex("^offer_[A-Za-z0-9_-]{12,80}$")

        /** 只接受本 App 发出的、格式正确且未过期的拒绝请求。 */
        internal fun declinedOffer(intent: Intent, now: Long): InboundOffer? {
            if (intent.action != ACTION_DECLINE_OFFER) return null
            val offerId = intent.getStringExtra(OfferAlerts.EXTRA_OFFER_ID)
            // data 与 extra 必须指向同一个来电，防止被覆盖的旧 PendingIntent 拒错对象。
            if (intent.data?.lastPathSegment != offerId) return null
            return declinedOffer(offerId, intent.getLongExtra(OfferAlerts.EXTRA_EXPIRES_AT, 0), now)
        }

        internal fun declinedOffer(offerId: String?, expiresAt: Long, now: Long): InboundOffer? {
            if (offerId == null || !OFFER_ID.matches(offerId) || expiresAt <= now) return null
            return InboundOffer(offerId, expiresAt)
        }
    }
}
