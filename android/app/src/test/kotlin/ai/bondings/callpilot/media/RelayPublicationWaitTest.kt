package ai.bondings.callpilot.media

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
class RelayPublicationWaitTest {
    @Test fun `default off still waits for complete even after relay is published`() = runTest {
        assertFalse(DirectSetupExperiments.POLL_PUBLISHED_RELAY)
        val complete = CompletableDeferred<Unit>()
        var relay = false
        val waiting = async { awaitRelayPublication(complete, { relay }, { false }, { 1_000 }) }
        runCurrent()
        relay = true
        advanceTimeBy(100)
        runCurrent()
        assertFalse(waiting.isCompleted)
        complete.complete(Unit)
        waiting.await()
    }

    @Test fun `enabled releases only when SDP relay is published and keeps timeout`() = runTest {
        var relay = false
        val waiting = async { awaitRelayPublication(CompletableDeferred(), { relay }, { false }, { 100 }, true) }
        runCurrent()
        advanceTimeBy(20)
        runCurrent()
        assertFalse(waiting.isCompleted)
        relay = true
        advanceTimeBy(10)
        runCurrent()
        assertTrue(waiting.isCompleted)
        waiting.await()
        val beforeTimeout = testScheduler.currentTime
        awaitRelayPublication(CompletableDeferred(), { false }, { false }, { 100 }, true)
        assertEquals(100, testScheduler.currentTime - beforeTimeout)
    }

    @Test fun `both modes release on complete and default mode retains deadline`() = runTest {
        for (enabled in listOf(false, true)) {
            val complete = CompletableDeferred<Unit>()
            var finished = false
            val waiting = async { awaitRelayPublication(complete, { false }, { finished }, { 100 }, enabled) }
            runCurrent()
            finished = true
            complete.complete(Unit)
            advanceTimeBy(10)
            runCurrent()
            waiting.await()
        }
        val beforeTimeout = testScheduler.currentTime
        awaitRelayPublication(CompletableDeferred(), { true }, { false }, { 100 })
        assertEquals(100, testScheduler.currentTime - beforeTimeout)
    }
}
