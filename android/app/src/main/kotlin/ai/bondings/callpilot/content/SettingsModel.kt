package ai.bondings.callpilot.content

import ai.bondings.callpilot.protocol.GatewayException
import ai.bondings.callpilot.protocol.HostedCloudException
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

enum class SettingsSyncStatus { IDLE, LOADING, LIVE, STALE, OFFLINE }

data class SettingsDeviceStatus(
    val edgeOnline: Boolean,
    val modemOnline: Boolean,
)

data class SettingsState(
    val deviceStatus: SettingsDeviceStatus? = null,
    val syncStatus: SettingsSyncStatus = SettingsSyncStatus.IDLE,
    val isRefreshing: Boolean = false,
    val isClearingContent: Boolean = false,
    val isUnpairing: Boolean = false,
)

class SettingsModel(
    private val fetchStatus: suspend () -> SettingsDeviceStatus,
    private val clearMessages: suspend () -> Unit,
    private val clearCalls: suspend () -> Unit,
    private val revokePairing: suspend () -> Unit,
    private val clearCredentials: () -> Unit,
    private val onUnpaired: () -> Unit,
) {
    private val mutableState = MutableStateFlow(SettingsState())
    val state: StateFlow<SettingsState> = mutableState.asStateFlow()
    private val generation = AtomicInteger(0)

    suspend fun refresh() {
        if (state.value.isRefreshing || state.value.isUnpairing) return
        val requestGeneration = generation.get()
        mutableState.value = state.value.copy(
            syncStatus = if (state.value.deviceStatus == null) {
                SettingsSyncStatus.LOADING
            } else {
                state.value.syncStatus
            },
            isRefreshing = true,
        )
        try {
            val status = fetchStatus()
            if (requestGeneration != generation.get()) return
            mutableState.value = state.value.copy(
                deviceStatus = status,
                syncStatus = SettingsSyncStatus.LIVE,
            )
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            if (isUnauthorized(error) && requestGeneration == generation.get()) {
                generation.incrementAndGet()
                runCatching { clearMessages() }
                runCatching { clearCalls() }
                clearCredentials()
                mutableState.value = SettingsState()
                onUnpaired()
            } else if (requestGeneration == generation.get()) {
                mutableState.value = state.value.copy(
                    syncStatus = if (state.value.deviceStatus == null) {
                        SettingsSyncStatus.OFFLINE
                    } else {
                        SettingsSyncStatus.STALE
                    },
                )
            }
        } finally {
            if (requestGeneration == generation.get()) {
                mutableState.value = state.value.copy(isRefreshing = false)
            }
        }
    }

    suspend fun clearLocalContent() {
        if (state.value.isClearingContent || state.value.isUnpairing) return
        mutableState.value = state.value.copy(isClearingContent = true)
        try {
            runCatching { clearMessages() }
            runCatching { clearCalls() }
        } finally {
            mutableState.value = state.value.copy(isClearingContent = false)
        }
    }

    suspend fun unpair() {
        if (state.value.isUnpairing || state.value.isClearingContent) return
        generation.incrementAndGet()
        mutableState.value = state.value.copy(isUnpairing = true)
        try {
            runCatching { revokePairing() }
            runCatching { clearMessages() }
            runCatching { clearCalls() }
        } finally {
            clearCredentials()
            mutableState.value = SettingsState()
            onUnpaired()
        }
    }

    private fun isUnauthorized(error: Exception): Boolean =
        (error as? HostedCloudException)?.errorCode == "UNAUTHORIZED" ||
            (error as? GatewayException)?.statusCode in setOf(401, 403)
}
