package ai.bondings.callpilot.content

import ai.bondings.callpilot.protocol.HostedCloudException
import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.yield
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SettingsModelTest {
    @Test
    fun `refresh publishes live Edge and modem status`() = runTest {
        val model = settingsModel(
            fetchStatus = { SettingsDeviceStatus(edgeOnline = true, modemOnline = false) },
        )

        model.refresh()

        assertEquals(SettingsSyncStatus.LIVE, model.state.value.syncStatus)
        assertEquals(SettingsDeviceStatus(edgeOnline = true, modemOnline = false), model.state.value.deviceStatus)
        assertFalse(model.state.value.isRefreshing)
    }

    @Test
    fun `failed refresh preserves last status as stale`() = runTest {
        var shouldFail = false
        val model = settingsModel(
            fetchStatus = {
                if (shouldFail) throw IOException("offline")
                SettingsDeviceStatus(edgeOnline = true, modemOnline = true)
            },
        )
        model.refresh()

        shouldFail = true
        model.refresh()

        assertEquals(SettingsSyncStatus.STALE, model.state.value.syncStatus)
        assertEquals(SettingsDeviceStatus(edgeOnline = true, modemOnline = true), model.state.value.deviceStatus)
        assertFalse(model.state.value.isRefreshing)
    }

    @Test
    fun `initial refresh failure is offline without fabricated device status`() = runTest {
        val model = settingsModel(fetchStatus = { throw IOException("offline") })

        model.refresh()

        assertEquals(SettingsSyncStatus.OFFLINE, model.state.value.syncStatus)
        assertNull(model.state.value.deviceStatus)
    }

    @Test
    fun `revoked device status clears local content and unpairs`() = runTest {
        var messagesCleared = 0
        var callsCleared = 0
        var credentialsCleared = 0
        var unpaired = 0
        val model = settingsModel(
            fetchStatus = { throw HostedCloudException(401, "UNAUTHORIZED", "revoked") },
            clearMessages = { messagesCleared += 1 },
            clearCalls = { callsCleared += 1 },
            clearCredentials = { credentialsCleared += 1 },
            onUnpaired = { unpaired += 1 },
        )

        model.refresh()

        assertEquals(1, messagesCleared)
        assertEquals(1, callsCleared)
        assertEquals(1, credentialsCleared)
        assertEquals(1, unpaired)
        assertEquals(SettingsState(), model.state.value)
    }

    @Test
    fun `clear local content preserves line status and clears both stores`() = runTest {
        var messagesCleared = 0
        var callsCleared = 0
        val model = settingsModel(
            fetchStatus = {
                SettingsDeviceStatus(edgeOnline = true, modemOnline = true)
            },
            clearMessages = { messagesCleared += 1 },
            clearCalls = { callsCleared += 1 },
        )
        model.refresh()

        model.clearLocalContent()

        assertEquals(1, messagesCleared)
        assertEquals(1, callsCleared)
        assertEquals(SettingsSyncStatus.LIVE, model.state.value.syncStatus)
        assertEquals(SettingsDeviceStatus(edgeOnline = true, modemOnline = true), model.state.value.deviceStatus)
        assertFalse(model.state.value.isClearingContent)
    }

    @Test
    fun `unpair fences a late status refresh`() = runTest {
        val started = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val model = settingsModel(
            fetchStatus = {
                started.complete(Unit)
                release.await()
                SettingsDeviceStatus(edgeOnline = true, modemOnline = true)
            },
        )

        val refresh = async { model.refresh() }
        started.await()
        model.unpair()
        release.complete(Unit)
        refresh.await()

        assertEquals(SettingsState(), model.state.value)
    }

    @Test
    fun `unpair clears local content and credentials even when remote revoke fails`() = runTest {
        var messagesCleared = 0
        var callsCleared = 0
        var credentialsCleared = 0
        var unpaired = 0
        val model = settingsModel(
            revokePairing = { throw IOException("offline") },
            clearMessages = { messagesCleared += 1 },
            clearCalls = { callsCleared += 1 },
            clearCredentials = { credentialsCleared += 1 },
            onUnpaired = { unpaired += 1 },
        )

        model.unpair()

        assertEquals(1, messagesCleared)
        assertEquals(1, callsCleared)
        assertEquals(1, credentialsCleared)
        assertEquals(1, unpaired)
        assertTrue(model.state.value == SettingsState())
    }

    @Test
    fun `unpair attempts both content stores when the first clear fails`() = runTest {
        var callsCleared = 0
        var credentialsCleared = 0
        var unpaired = 0
        val model = settingsModel(
            clearMessages = { throw IOException("delete failed") },
            clearCalls = { callsCleared += 1 },
            clearCredentials = { credentialsCleared += 1 },
            onUnpaired = { unpaired += 1 },
        )

        model.unpair()

        assertEquals(1, callsCleared)
        assertEquals(1, credentialsCleared)
        assertEquals(1, unpaired)
    }

    @Test
    fun `unpair does not race a local content clear`() = runTest {
        val clearStarted = CompletableDeferred<Unit>()
        val releaseClear = CompletableDeferred<Unit>()
        var credentialsCleared = 0
        var unpaired = 0
        val model = settingsModel(
            clearMessages = {
                clearStarted.complete(Unit)
                releaseClear.await()
            },
            clearCredentials = { credentialsCleared += 1 },
            onUnpaired = { unpaired += 1 },
        )

        val clear = async { model.clearLocalContent() }
        clearStarted.await()
        val unpair = async { model.unpair() }
        yield()

        assertEquals(0, credentialsCleared)
        assertEquals(0, unpaired)
        releaseClear.complete(Unit)
        clear.await()
        unpair.await()
        assertEquals(0, credentialsCleared)
        assertEquals(0, unpaired)
    }

    private fun settingsModel(
        fetchStatus: suspend () -> SettingsDeviceStatus = {
            SettingsDeviceStatus(edgeOnline = false, modemOnline = false)
        },
        clearMessages: suspend () -> Unit = {},
        clearCalls: suspend () -> Unit = {},
        revokePairing: suspend () -> Unit = {},
        clearCredentials: () -> Unit = {},
        onUnpaired: () -> Unit = {},
    ) = SettingsModel(
        fetchStatus = fetchStatus,
        clearMessages = clearMessages,
        clearCalls = clearCalls,
        revokePairing = revokePairing,
        clearCredentials = clearCredentials,
        onUnpaired = onUnpaired,
    )
}
