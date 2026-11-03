package ai.bondings.callpilot.call

import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.HostedCallSession
import ai.bondings.callpilot.protocol.HostedCloudClient
import ai.bondings.callpilot.protocol.HostedCloudException
import ai.bondings.callpilot.protocol.IceServer
import ai.bondings.callpilot.protocol.OutboundCall
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.async

/** Process memory only; generation fencing prevents an unpaired in-flight fetch from repopulating it. */
class OutboundIceCache(
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
    private val clockMs: () -> Long = System::currentTimeMillis,
) {
    private var generation = 0L
    private var owner: StoredPairing? = null
    private var cached: Pair<List<IceServer>, Long>? = null
    private var pending: Deferred<List<IceServer>>? = null

    @Synchronized fun activate(pairing: StoredPairing?) {
        if (owner != pairing || pairing == null) {
            generation++
            owner = pairing
            cached = null
            pending = null
        }
    }

    suspend fun get(pairing: StoredPairing, load: suspend () -> Pair<List<IceServer>, Long>): List<IceServer> {
        val task = synchronized(this) {
            cached?.takeIf { owner == pairing && it.second - clockMs() > 60_000 }?.let { return it.first }
            pending?.takeIf { owner == pairing && !it.isCompleted } ?: run {
                val version = generation
                // The shared request outlives a cancelled page/foreground waiter.
                scope.async(start = CoroutineStart.LAZY) {
                    val result = load()
                    synchronized(this@OutboundIceCache) {
                        // Keep 24h maximum-call headroom and fence stale pairing completions.
                        if (owner == pairing && generation == version && result.second - clockMs() > 60_000) {
                            cached = result.first to minOf(result.second, clockMs() + 120_000)
                        }
                    }
                    result.first
                }.also { if (owner == pairing) pending = it }
            }
        }
        task.start()
        return task.await()
    }
}

object OutboundIcePrefetch { val cache = OutboundIceCache() }

/** A8：外呼直连的中转接口（TURN / 带 offer 建呼叫 / 轮询 / 回落），抽成接口便于状态机单测。 */
interface OutboundMediaGateway {
    suspend fun iceServers(edgeId: String): List<IceServer>
    suspend fun create(edgeId: String, idempotencyKey: String, sdpOffer: String): OutboundCall
    suspend fun awaitSession(edgeId: String, call: OutboundCall): HostedCallSession
    suspend fun fallbackLiveKit(callId: String): HostedCallSession
}

class HostedOutboundMediaGateway(
    private val pairing: StoredPairing,
    private val io: CoroutineDispatcher = Dispatchers.IO,
) : OutboundMediaGateway {
    private fun client(): HostedCloudClient =
        HostedCloudClient(pairing.gatewayUrl).also { it.credential = pairing.credential }

    override suspend fun iceServers(edgeId: String): List<IceServer> =
        OutboundIcePrefetch.cache.get(pairing) { withContext(io) { client().outboundIceCredentials(edgeId) } }

    override suspend fun create(edgeId: String, idempotencyKey: String, sdpOffer: String): OutboundCall =
        withContext(io) { client().createDirectCall(edgeId, sdpOffer, idempotencyKey) }

    override suspend fun awaitSession(edgeId: String, call: OutboundCall): HostedCallSession =
        withContext(io) { client().awaitCallSession(edgeId, call) }

    /** 中转媒体通道暂不可用（503 / 504）时同请求重试一次；Edge 对同一房间的回落幂等。 */
    override suspend fun fallbackLiveKit(callId: String): HostedCallSession = withContext(io) {
        try {
            client().fallbackCallLiveKit(callId)
        } catch (error: HostedCloudException) {
            if (error.statusCode == 503 || error.statusCode == 504) client().fallbackCallLiveKit(callId) else throw error
        }
    }
}
