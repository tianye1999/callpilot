package ai.bondings.callpilot.call

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class OfferDeclineReceiverTest {
    @Test
    fun `accepts only well-formed unexpired offers`() {
        val offer = OfferDeclineReceiver.declinedOffer("offer_abcdefghijkl", 2_000, 1_000)
        assertEquals("offer_abcdefghijkl", offer?.offerId)
        assertNull(OfferDeclineReceiver.declinedOffer("offer_abcdefghijkl", 1_000, 1_000))
        assertNull(OfferDeclineReceiver.declinedOffer("not-an-offer", 2_000, 1_000))
        assertNull(OfferDeclineReceiver.declinedOffer(null, 2_000, 1_000))
    }
}
