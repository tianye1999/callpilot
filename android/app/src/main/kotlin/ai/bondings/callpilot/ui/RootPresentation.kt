package ai.bondings.callpilot.ui

import ai.bondings.callpilot.call.CallState
import ai.bondings.callpilot.protocol.InboundOffer

/** Root rendering priority. The paired main shell stays mounted behind overlays. */
sealed interface RootPresentation {
    data object Pairing : RootPresentation
    data object Main : RootPresentation
    data class IncomingOffer(val offer: InboundOffer) : RootPresentation
    data object Call : RootPresentation

    companion object {
        fun resolve(
            isPaired: Boolean,
            callState: CallState,
            incomingOffer: InboundOffer?,
        ): RootPresentation {
            if (!isPaired) return Pairing
            if (callState !is CallState.Idle) return Call
            return incomingOffer?.let(::IncomingOffer) ?: Main
        }
    }
}
