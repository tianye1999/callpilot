package ai.bondings.callpilot.call

import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.HostedCloudClient
import ai.bondings.callpilot.protocol.HostedDeviceStatus
import ai.bondings.callpilot.protocol.HttpClients
import ai.bondings.callpilot.protocol.PairingProtocol
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

data class DirectFeatures(val outboundDirectV1: Boolean = false, val inboundFastpathV2: Boolean = false)

/** 缓存按配对隔离；失败保留旧值，旧请求不能在解除/重新配对后写回。 */
class DirectCapabilityCache(
    private val scope: CoroutineScope,
    private val loader: suspend (StoredPairing) -> HostedDeviceStatus?,
) {
    private var pairing: StoredPairing? = null
    private var generation = 0L
    private var value: DirectFeatures? = null
    private var refreshing: Job? = null
    private val mutableFeatures = MutableStateFlow(DirectFeatures())
    val features = mutableFeatures.asStateFlow()

    @Synchronized
    fun activate(current: StoredPairing?) {
        if (pairing != current) {
            generation++
            refreshing?.cancel()
            refreshing = null
            pairing = current
            value = null
            mutableFeatures.value = DirectFeatures()
        }
        current?.let { refresh(it) }
    }

    @Synchronized
    fun cached(current: StoredPairing): DirectFeatures {
        if (pairing != current) return DirectFeatures()
        val snapshot = value
        if (snapshot == null) refresh(current)
        return snapshot ?: DirectFeatures()
    }

    @Synchronized
    fun refresh(current: StoredPairing) {
        if (pairing != current || current.protocol != PairingProtocol.HOSTED || refreshing?.isActive == true) return
        val epoch = generation
        refreshing = scope.launch(start = CoroutineStart.LAZY) {
            val status = try {
                loader(current)
            } catch (error: CancellationException) {
                throw error
            } catch (_: Exception) {
                null
            }
            if (status != null) synchronized(this@DirectCapabilityCache) {
                if (generation == epoch && pairing == current) {
                    value = DirectFeatures(status.outboundDirectV1, status.inboundFastpathV2)
                    mutableFeatures.value = value!!
                }
            }
        }.also { it.start() }
    }
}

/** 与 iPhone 共用 /v1/device 能力；网络读取仅由缓存的后台刷新调用。 */
object DirectCallCapabilities {
    val cache = DirectCapabilityCache(CoroutineScope(SupervisorJob() + Dispatchers.Default), ::load)

    suspend fun load(pairing: StoredPairing): HostedDeviceStatus? {
        if (pairing.protocol != PairingProtocol.HOSTED) return null
        return try {
            withContext(Dispatchers.IO) {
                HostedCloudClient(pairing.gatewayUrl, HttpClients.quick)
                    .also { it.credential = pairing.credential }.deviceStatus()
            }
        } catch (error: CancellationException) {
            throw error
        } catch (_: Exception) {
            null
        }
    }
}
