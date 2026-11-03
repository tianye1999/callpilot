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
        withContext(io) { client().outboundIceServers(edgeId) }

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
