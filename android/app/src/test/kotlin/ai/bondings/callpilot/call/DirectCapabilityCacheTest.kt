package ai.bondings.callpilot.call

import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.DeviceCredential
import ai.bondings.callpilot.protocol.HostedDeviceStatus
import ai.bondings.callpilot.protocol.PairingProtocol
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.*
import org.junit.Test

class DirectCapabilityCacheTest {
    private val paired = StoredPairing("https://relay.example", "test", DeviceCredential("device_abcdefghijkl", "secret"), PairingProtocol.HOSTED, "edge_abcdefghijkl")
    private fun status(enabled: Boolean) = HostedDeviceStatus(
        false, null, JsonObject(emptyMap()),
        Json.parseToJsonElement("""{"outboundDirectV1":$enabled,"inboundFastpathV2":$enabled}""") as JsonObject,
    )

    @Test fun `failed refresh preserves old value and first failure stays false`() = runTest {
        var result: HostedDeviceStatus? = null
        var fail = false
        val cache = DirectCapabilityCache(backgroundScope) {
            if (fail) error("offline")
            result
        }
        cache.activate(paired)
        runCurrent()
        assertEquals(DirectFeatures(), cache.cached(paired))
        result = status(true)
        runCurrent()
        assertEquals(DirectFeatures(true, true), cache.cached(paired))
        fail = true
        cache.refresh(paired)
        runCurrent()
        assertEquals(DirectFeatures(true, true), cache.cached(paired))
        fail = false
        result = status(false)
        cache.refresh(paired)
        runCurrent()
        assertEquals(DirectFeatures(), cache.cached(paired))
    }

    @Test fun `unpair and re-pair reject late results even for the same credential`() = runTest {
        val late = CompletableDeferred<HostedDeviceStatus>()
        var requests = 0
        val cache = DirectCapabilityCache(backgroundScope) {
            requests++
            if (requests == 1) withContext(NonCancellable) { late.await() } else status(false)
        }
        cache.activate(paired)
        runCurrent()
        cache.cached(paired)
        cache.cached(paired)
        runCurrent()
        assertEquals(1, requests)
        cache.activate(null)
        assertEquals(DirectFeatures(), cache.cached(paired))
        cache.activate(paired)
        runCurrent()
        late.complete(status(true))
        runCurrent()
        assertEquals(DirectFeatures(), cache.cached(paired))
        val other = paired.copy(edgeId = "edge_otherdevice")
        cache.activate(other)
        assertEquals(DirectFeatures(), cache.cached(paired))
        runCurrent()
        assertEquals(DirectFeatures(), cache.cached(other))
    }

    @Test fun `unpair clears successful cache and old reads cannot refresh it`() = runTest {
        var requests = 0
        val cache = DirectCapabilityCache(backgroundScope) { requests++; status(true) }
        cache.activate(paired)
        runCurrent()
        assertEquals(DirectFeatures(true, true), cache.cached(paired))
        cache.activate(null)
        assertEquals(DirectFeatures(), cache.features.value)
        assertEquals(DirectFeatures(), cache.cached(paired))
        runCurrent()
        assertEquals(1, requests)
    }
}
