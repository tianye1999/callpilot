package ai.bondings.callpilot.protocol

import okhttp3.OkHttpClient

/** HTTP control protocol selected when the phone was paired. */
enum class PairingProtocol(val storedValue: String) {
    TUNNEL("tunnel"),
    HOSTED("hosted");

    companion object {
        fun fromStored(value: String?): PairingProtocol =
            entries.firstOrNull { it.storedValue == value } ?: TUNNEL
    }
}

data class NegotiatedPairing(
    val credential: DeviceCredential,
    val protocol: PairingProtocol,
    val edgeId: String? = null,
)

/** Pair on one origin, preferring hosted v1 and falling back only when that route is absent. */
class PairingNegotiator(
    private val baseUrl: String,
    private val client: OkHttpClient = OkHttpClient(),
) {
    fun pair(
        code: String,
        displayName: String,
        preferredProtocol: PairingProtocol? = null,
    ): NegotiatedPairing = when (preferredProtocol) {
        PairingProtocol.HOSTED -> pairHosted(code, displayName)
        PairingProtocol.TUNNEL -> pairTunnel(code, displayName)
        null -> try {
            pairHosted(code, displayName)
        } catch (e: HostedCloudException) {
            if (e.statusCode != 404 && e.statusCode != 405) throw e
            pairTunnel(code, displayName)
        }
    }

    private fun pairHosted(code: String, displayName: String): NegotiatedPairing {
        val result = HostedCloudClient(baseUrl, client).claimPairing(code, displayName)
        return NegotiatedPairing(
            credential = result.credential,
            protocol = PairingProtocol.HOSTED,
            edgeId = result.device.edgeId,
        )
    }

    private fun pairTunnel(code: String, displayName: String): NegotiatedPairing {
        val result = GatewayClient(baseUrl, client).pair(code, displayName)
        return NegotiatedPairing(result.credential, PairingProtocol.TUNNEL)
    }
}
