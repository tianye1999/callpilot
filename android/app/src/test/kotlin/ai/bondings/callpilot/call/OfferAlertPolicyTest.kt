package ai.bondings.callpilot.call

import ai.bondings.callpilot.protocol.InboundOffer
import org.junit.Assert.*
import org.junit.Test

class OfferAlertPolicyTest {
    private val offer = InboundOffer("offer_test_00000001", 20_000)

    @Test
    fun `only new unexpired offers ring once even after disappearing`() {
        val policy = OfferAlertPolicy()
        assertEquals(listOf(offer), policy.update(listOf(offer), 1).notify)
        assertTrue(policy.update(listOf(offer), 2).notify.isEmpty())
        assertEquals(setOf(offer.offerId), policy.update(emptyList(), 3).cancel)
        assertTrue(policy.update(listOf(offer), 4).notify.isEmpty())
        assertTrue(policy.update(listOf(offer), offer.expiresAt).notify.isEmpty())
    }

    @Test
    fun `foreground suppresses ringing but an unseen offer can ring after leaving`() {
        val policy = OfferAlertPolicy()
        assertTrue(policy.update(listOf(offer), 1, foreground = true).notify.isEmpty())
        assertEquals(listOf(offer), policy.update(listOf(offer), 2).notify)
        assertTrue(policy.update(listOf(offer), 3, foreground = true).cancel.isEmpty())
        assertTrue(policy.update(listOf(offer), 4).notify.isEmpty())
    }

    @Test
    fun `presenting in app prevents a later ring without hiding answer screen`() {
        val policy = OfferAlertPolicy()
        policy.presented(offer)
        assertTrue(policy.update(listOf(offer), 1).notify.isEmpty())
        assertFalse(policy.isDismissed(offer.offerId))
    }

    @Test
    fun `accept or decline cancels and suppresses stale results`() {
        val policy = OfferAlertPolicy()
        policy.update(listOf(offer), 1)
        policy.dismiss(offer)
        assertEquals(setOf(offer.offerId), policy.update(listOf(offer), 2).cancel)
        assertTrue(policy.isDismissed(offer.offerId))
        assertTrue(policy.update(listOf(offer), 3).notify.isEmpty())
    }

    @Test
    fun `busy call and local expiry cancel even without another network response`() {
        val policy = OfferAlertPolicy()
        policy.update(listOf(offer), 1)
        assertEquals(setOf(offer.offerId), policy.update(listOf(offer), 2, busy = true).cancel)
        assertTrue(policy.update(listOf(offer), 3).notify.isEmpty())
        val other = offer.copy(offerId = "offer_test_00000002")
        policy.update(listOf(other), 4)
        assertEquals(setOf(other.offerId), policy.update(listOf(other), other.expiresAt).cancel)
        assertTrue(policy.snapshot().isEmpty())
    }

    @Test
    fun `permission denial does not consume an alert`() {
        val policy = OfferAlertPolicy()
        assertTrue(policy.update(listOf(offer), 1, notificationsAllowed = false).notify.isEmpty())
        assertEquals(listOf(offer), policy.update(listOf(offer), 2).notify)
    }

    @Test
    fun `persisted decisions survive process recreation`() {
        val policy = OfferAlertPolicy()
        policy.update(listOf(offer), 1)
        val declined = offer.copy(offerId = "offer_test_00000002")
        policy.dismiss(declined)
        val restored = OfferAlertPolicy(policy.snapshot())
        assertTrue(restored.update(listOf(offer, declined), 2).notify.isEmpty())
        assertTrue(restored.isDismissed(declined.offerId))
        assertFalse(restored.isDismissed(offer.offerId))
    }

    @Test
    fun `duplicate server entries only emit one alert`() {
        assertEquals(listOf(offer), OfferAlertPolicy().update(listOf(offer, offer), 1).notify)
    }

    @Test
    fun `local timer cancels expired alerts but never rings a stale snapshot`() {
        val policy = OfferAlertPolicy()
        assertTrue(policy.update(listOf(offer), 1, allowNew = false).notify.isEmpty())
        policy.update(listOf(offer), 2)
        assertTrue(policy.update(listOf(offer), 3, allowNew = false).cancel.isEmpty())
        assertEquals(setOf(offer.offerId), policy.update(listOf(offer), 20_000, allowNew = false).cancel)
    }

    @Test
    fun `failed notification can retry but cannot undo a user dismissal`() {
        val policy = OfferAlertPolicy()
        policy.update(listOf(offer), 1)
        policy.notificationFailed(offer.offerId)
        assertEquals(listOf(offer), policy.update(listOf(offer), 2).notify)
        policy.dismiss(offer)
        policy.notificationFailed(offer.offerId)
        assertTrue(policy.update(listOf(offer), 3).notify.isEmpty())
        assertTrue(policy.isDismissed(offer.offerId))
    }

    @Test
    fun `full screen launch keeps the existing ring until the owner acts`() {
        val policy = OfferAlertPolicy()
        policy.update(listOf(offer), 1)
        policy.presented(offer)
        val resumed = policy.update(listOf(offer), 2, foreground = true)
        assertTrue(resumed.cancel.isEmpty())
        assertTrue(resumed.notify.isEmpty())
        policy.dismiss(offer)
        assertEquals(setOf(offer.offerId), policy.update(listOf(offer), 3, foreground = true).cancel)
    }
}
