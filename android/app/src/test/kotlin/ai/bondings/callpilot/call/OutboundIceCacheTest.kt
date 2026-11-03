package ai.bondings.callpilot.call

import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.DeviceCredential
import ai.bondings.callpilot.protocol.PairingProtocol
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test

class OutboundIceCacheTest {
    @Test fun `route cancellation does not cancel the shared prefetch`() = runTest {
        val cache = OutboundIceCache { 1_000 }
        cache.activate(pairing)
        val started = CompletableDeferred<Unit>()
        val finish = CompletableDeferred<Unit>()
        var requests = 0
        val first = async { cache.get(pairing) {
            requests++; started.complete(Unit); finish.await()
            emptyList<ai.bondings.callpilot.protocol.IceServer>() to 121_000L
        } }
        started.await()
        first.cancelAndJoin()
        val nextPage = async { cache.get(pairing) {
            requests++; emptyList<ai.bondings.callpilot.protocol.IceServer>() to 121_000L
        } }
        finish.complete(Unit)
        nextPage.await()
        cache.get(pairing) { error("A foreground cache hit must not fetch again") }
        assertEquals(1, requests)
    }
    private val pairing = StoredPairing("https://relay.test", "Test", DeviceCredential("device_abcdefghijkl", "synthetic"),
        PairingProtocol.HOSTED, "edge_abcdefghijkl")

    @Test fun `hit expiry boundary and unpair invalidation`() = runTest {
        var now = 1_000L
        val cache = OutboundIceCache { now }
        cache.activate(pairing)
        var requests = 0
        suspend fun fetch() = cache.get(pairing) { requests++; emptyList<ai.bondings.callpilot.protocol.IceServer>() to 121_000L }
        fetch(); fetch()
        assertEquals(1, requests)
        now = 61_000
        fetch()
        assertEquals(2, requests)
        cache.activate(null)
        cache.activate(pairing)
        now = 1_000
        fetch()
        assertEquals(3, requests)
    }

    @Test fun `in flight unpair cannot repopulate and concurrent callers coalesce`() = runTest {
        val cache = OutboundIceCache { 1_000 }
        cache.activate(pairing)
        val started = CompletableDeferred<Unit>()
        val finish = CompletableDeferred<Unit>()
        var requests = 0
        val first = async { cache.get(pairing) { requests++; started.complete(Unit); finish.await(); emptyList<ai.bondings.callpilot.protocol.IceServer>() to 121_000L } }
        started.await()
        val second = async { cache.get(pairing) { requests++; emptyList<ai.bondings.callpilot.protocol.IceServer>() to 121_000L } }
        finish.complete(Unit)
        first.await(); second.await()
        assertEquals(1, requests)
        cache.activate(null)
        cache.activate(pairing)
        val late = async { cache.get(pairing) { cache.activate(null); emptyList<ai.bondings.callpilot.protocol.IceServer>() to 121_000L } }
        late.await()
        cache.activate(pairing)
        cache.get(pairing) { requests++; emptyList<ai.bondings.callpilot.protocol.IceServer>() to 121_000L }
        assertEquals(2, requests)
    }

    @Test fun `long TTL is capped to one minute and failed prefetch is retryable`() = runTest {
        var now = 1_000L
        val cache = OutboundIceCache { now }
        cache.activate(pairing)
        var requests = 0
        runCatching { cache.get(pairing) { requests++; error("synthetic") } }
        suspend fun fetch() = cache.get(pairing) { requests++; emptyList<ai.bondings.callpilot.protocol.IceServer>() to 86_521_000L }
        fetch(); fetch()
        assertEquals(2, requests)
        now = 61_000
        fetch()
        assertEquals(3, requests)
    }
}
