package ai.bondings.callpilot.call

import ai.bondings.callpilot.protocol.InboundOffer
import kotlinx.serialization.Serializable

@Serializable
internal data class OfferAlertRecord(val expiresAt: Long, val dismissed: Boolean = false)

internal data class OfferAlertChanges(
    val notify: List<InboundOffer>,
    val cancel: Set<String>,
)

/** 前台展示和后台通知共用去重；网络短暂漏掉 offer 不能让它再次响铃。 */
internal class OfferAlertPolicy(saved: Map<String, OfferAlertRecord> = emptyMap()) {
    private val handled = saved.toMutableMap()
    private val active = mutableSetOf<String>()

    fun snapshot(): Map<String, OfferAlertRecord> = handled.toMap()

    fun isDismissed(offerId: String): Boolean = handled[offerId]?.dismissed == true

    fun notificationFailed(offerId: String) {
        if (!isDismissed(offerId)) handled.remove(offerId)
        active.remove(offerId)
    }

    fun presented(offer: InboundOffer) {
        handled[offer.offerId] = OfferAlertRecord(
            maxOf(offer.expiresAt, handled[offer.offerId]?.expiresAt ?: 0),
            isDismissed(offer.offerId),
        )
    }

    fun dismiss(offer: InboundOffer) {
        presented(offer)
        handled[offer.offerId] = handled.getValue(offer.offerId).copy(dismissed = true)
    }

    fun update(
        offers: List<InboundOffer>,
        now: Long,
        foreground: Boolean = false,
        busy: Boolean = false,
        notificationsAllowed: Boolean = true,
        allowNew: Boolean = true,
    ): OfferAlertChanges {
        handled.entries.removeAll { it.value.expiresAt <= now }
        val valid = offers.distinctBy { it.offerId }.filter {
            it.expiresAt > now && !isDismissed(it.offerId)
        }
        // 全屏通知会让 Activity 自动 RESUMED，不能因此截断已经响起的来电。
        val eligible = if (busy || !notificationsAllowed) emptyList() else valid
        val eligibleIds = eligible.map { it.offerId }.toSet()
        val cancel = active.filter { it !in eligibleIds }.toSet()
        active.removeAll(cancel)
        val notify = if (allowNew && !foreground) eligible.filter { it.offerId !in handled } else emptyList()
        notify.forEach {
            presented(it)
            active += it.offerId
        }
        return OfferAlertChanges(notify, cancel)
    }
}
