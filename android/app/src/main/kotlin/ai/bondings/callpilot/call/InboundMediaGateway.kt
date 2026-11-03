package ai.bondings.callpilot.call

import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.HostedCallSession
import ai.bondings.callpilot.protocol.HostedCloudClient
import ai.bondings.callpilot.protocol.HostedCloudException
import ai.bondings.callpilot.protocol.IceServer
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** 来电接管的中转媒体接口（claim / TURN / 补 SDP / 回落 / 结束），抽成接口便于状态机单测。 */
interface InboundMediaGateway {
    suspend fun claim(offerId: String, direct: Boolean, sdpOffer: String? = null): HostedCallSession
    suspend fun iceServers(offerId: String): List<IceServer>
    suspend fun completeSdp(offerId: String, claimId: String, sdpOffer: String): HostedCallSession
    suspend fun fallbackLiveKit(offerId: String, claimId: String): HostedCallSession
    suspend fun end(offerId: String, claimId: String)
}

class HostedInboundMediaGateway(
    private val pairing: StoredPairing,
    private val io: CoroutineDispatcher = Dispatchers.IO,
) : InboundMediaGateway {
    private fun client(): HostedCloudClient =
        HostedCloudClient(pairing.gatewayUrl).also { it.credential = pairing.credential }

    override suspend fun claim(offerId: String, direct: Boolean, sdpOffer: String?): HostedCallSession =
        withContext(io) { client().claimInboundOffer(offerId, sdpOffer = sdpOffer, direct = direct) }

    override suspend fun iceServers(offerId: String): List<IceServer> =
        withContext(io) { client().inboundIceServers(offerId) }

    override suspend fun completeSdp(offerId: String, claimId: String, sdpOffer: String): HostedCallSession =
        withContext(io) { client().completeInboundSdp(offerId, claimId, sdpOffer) }

    /** 中转媒体通道暂不可用（503 / 504）时同请求重试一次，绝不重新 claim。 */
    override suspend fun fallbackLiveKit(offerId: String, claimId: String): HostedCallSession = withContext(io) {
        try {
            client().fallbackInboundLiveKit(offerId, claimId)
        } catch (error: HostedCloudException) {
            if (error.statusCode == 503 || error.statusCode == 504) client().fallbackInboundLiveKit(offerId, claimId) else throw error
        }
    }

    override suspend fun end(offerId: String, claimId: String) = withContext(io) {
        try {
            client().endInboundClaim(offerId, claimId)
        } catch (error: HostedCloudException) {
            if (error.statusCode == 503) client().endInboundClaim(offerId, claimId) else throw error
        }
    }
}
