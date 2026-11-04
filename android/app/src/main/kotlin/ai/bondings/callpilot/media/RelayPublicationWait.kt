package ai.bondings.callpilot.media

import kotlinx.coroutines.Deferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull

object DirectSetupExperiments {
    // Device acceptance required. Does not accept srflx-only SDP or change the gather deadline.
    const val POLL_PUBLISHED_RELAY = false
}

internal suspend fun awaitRelayPublication(
    complete: Deferred<Unit>,
    hasPublishedRelay: () -> Boolean,
    isComplete: () -> Boolean,
    remainingMs: () -> Long,
    enabled: Boolean = DirectSetupExperiments.POLL_PUBLISHED_RELAY,
) {
    withTimeoutOrNull(remainingMs()) {
        if (!enabled) complete.await()
        else while (!hasPublishedRelay() && !isComplete()) delay(10)
    }
}
