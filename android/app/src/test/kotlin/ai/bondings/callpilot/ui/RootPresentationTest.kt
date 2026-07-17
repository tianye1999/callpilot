package ai.bondings.callpilot.ui

import ai.bondings.callpilot.call.CallState
import ai.bondings.callpilot.protocol.InboundOffer
import org.junit.Assert.assertEquals
import org.junit.Test

class RootPresentationTest {
    private val offer = InboundOffer(
        offerId = "offer_abcdefghijkl",
        expiresAt = 9_999L,
    )

    @Test
    fun `unpaired presentation overrides call and offer`() {
        assertEquals(
            RootPresentation.Pairing,
            RootPresentation.resolve(
                isPaired = false,
                callState = CallState.InCall("active"),
                incomingOffer = offer,
            ),
        )
    }

    @Test
    fun `call presentation overrides incoming offer`() {
        assertEquals(
            RootPresentation.Call,
            RootPresentation.resolve(
                isPaired = true,
                callState = CallState.WaitingMedia("active"),
                incomingOffer = offer,
            ),
        )
    }

    @Test
    fun `terminal call result remains presented until acknowledged`() {
        val terminalStates = listOf(
            CallState.Ended("completed", "remote_hangup"),
            CallState.Failed("failed", "network", "MEDIA_FAILED"),
        )

        terminalStates.forEach { state ->
            assertEquals(
                RootPresentation.Call,
                RootPresentation.resolve(
                    isPaired = true,
                    callState = state,
                    incomingOffer = offer,
                ),
            )
        }
    }

    @Test
    fun `idle presentation shows offer then main when offer clears`() {
        assertEquals(
            RootPresentation.IncomingOffer(offer),
            RootPresentation.resolve(
                isPaired = true,
                callState = CallState.Idle,
                incomingOffer = offer,
            ),
        )
        assertEquals(
            RootPresentation.Main,
            RootPresentation.resolve(
                isPaired = true,
                callState = CallState.Idle,
                incomingOffer = null,
            ),
        )
    }
}
