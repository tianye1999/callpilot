package ai.bondings.callpilot.ui

import ai.bondings.callpilot.R
import ai.bondings.callpilot.call.CallManager
import ai.bondings.callpilot.content.CallHistoryCacheStore
import ai.bondings.callpilot.content.CallHistoryModel
import ai.bondings.callpilot.content.CallHistoryState
import ai.bondings.callpilot.content.MessageCacheStore
import ai.bondings.callpilot.content.MessageInboxState
import ai.bondings.callpilot.content.MessageInboxModel
import ai.bondings.callpilot.content.ProtectedJsonStore
import ai.bondings.callpilot.content.SettingsDeviceStatus
import ai.bondings.callpilot.content.SettingsModel
import ai.bondings.callpilot.pairing.CredentialStore
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.DeviceStatus
import ai.bondings.callpilot.protocol.GatewayClient
import ai.bondings.callpilot.protocol.HostedCloudClient
import ai.bondings.callpilot.protocol.HostedDeviceStatus
import ai.bondings.callpilot.protocol.PairingProtocol
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Email
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.annotation.StringRes
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.withContext

enum class MainTab(
    val route: String,
    @param:StringRes val labelRes: Int,
    val icon: ImageVector,
) {
    Dial("dial", R.string.tab_dial, Icons.Filled.Call),
    Records("records", R.string.tab_records, Icons.Filled.DateRange),
    Messages("messages", R.string.tab_messages, Icons.Filled.Email),
    Settings("settings", R.string.tab_settings, Icons.Filled.Settings),
}

/** Paired app shell. Top-level navigation survives call and offer overlays. */
@Composable
fun MainTabShell(
    pairing: StoredPairing,
    store: CredentialStore,
    manager: CallManager,
    onUnpaired: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val navController = rememberNavController()
    val backStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = backStackEntry?.destination?.route ?: MainTab.Dial.route
    val tunnelClient = remember(pairing) {
        if (pairing.protocol == PairingProtocol.TUNNEL) GatewayClient(pairing.gatewayUrl).also {
            it.credential = pairing.credential
        } else null
    }
    val hostedClient = remember(pairing) {
        if (pairing.protocol == PairingProtocol.HOSTED) HostedCloudClient(pairing.gatewayUrl).also {
            it.credential = pairing.credential
        } else null
    }
    val handleUnauthorized = remember(pairing) {
        {
            ProtectedJsonStore.clearAll(context)
            store.clear()
            onUnpaired()
        }
    }
    val messageModel = remember(pairing, hostedClient) {
        hostedClient?.let { client ->
            MessageInboxModel(
                client = client,
                store = MessageCacheStore(ProtectedJsonStore.messages(context)),
                deviceId = pairing.credential.deviceId,
                onUnauthorized = handleUnauthorized,
            )
        }
    }
    val callHistoryModel = remember(pairing, hostedClient) {
        hostedClient?.let { client ->
            CallHistoryModel(
                client = client,
                store = CallHistoryCacheStore(ProtectedJsonStore.callHistory(context)),
                deviceId = pairing.credential.deviceId,
                onUnauthorized = handleUnauthorized,
            )
        }
    }
    val emptyMessageState = remember { MutableStateFlow(MessageInboxState()) }
    val emptyCallHistoryState = remember { MutableStateFlow(CallHistoryState()) }
    val messageState by (messageModel?.state ?: emptyMessageState).collectAsState()
    val callHistoryState by (callHistoryModel?.state ?: emptyCallHistoryState).collectAsState()
    val settingsModel = remember(pairing, tunnelClient, hostedClient, messageModel, callHistoryModel) {
        SettingsModel(
            fetchStatus = {
                withContext(Dispatchers.IO) {
                    when (pairing.protocol) {
                        PairingProtocol.TUNNEL -> checkNotNull(tunnelClient).deviceStatus().let {
                            it.toSettingsDeviceStatus()
                        }
                        PairingProtocol.HOSTED -> checkNotNull(hostedClient).deviceStatus().let {
                            it.toSettingsDeviceStatus()
                        }
                    }
                }
            },
            clearMessages = {
                messageModel?.clearLocalData()
                    ?: withContext(Dispatchers.IO) { ProtectedJsonStore.messages(context).clear() }
            },
            clearCalls = {
                callHistoryModel?.clearLocalData()
                    ?: withContext(Dispatchers.IO) { ProtectedJsonStore.callHistory(context).clear() }
            },
            revokePairing = {
                withContext(Dispatchers.IO) {
                    when (pairing.protocol) {
                        PairingProtocol.TUNNEL -> checkNotNull(tunnelClient).unpair()
                        PairingProtocol.HOSTED -> checkNotNull(hostedClient).unpair()
                    }
                }
            },
            clearCredentials = store::clear,
            onUnpaired = onUnpaired,
        )
    }
    LaunchedEffect(messageModel) {
        messageModel?.loadCachedContent()
    }
    LaunchedEffect(callHistoryModel) {
        callHistoryModel?.loadCachedContent()
    }

    Scaffold(
        modifier = modifier,
        bottomBar = {
            NavigationBar {
                MainTab.entries.forEach { tab ->
                    NavigationBarItem(
                        selected = isMainTabSelected(tab, currentRoute),
                        onClick = {
                            navController.navigate(tab.route) {
                                popUpTo(navController.graph.findStartDestination().id) {
                                    saveState = true
                                }
                                launchSingleTop = true
                                restoreState = true
                            }
                        },
                        icon = {
                            BadgedBox(
                                badge = {
                                    if (tab == MainTab.Messages && messageState.unreadCount > 0) {
                                        Badge { Text(messageState.unreadCount.toString()) }
                                    }
                                },
                            ) {
                                Icon(
                                    imageVector = tab.icon,
                                    contentDescription = null,
                                )
                            }
                        },
                        label = { Text(stringResource(tab.labelRes)) },
                    )
                }
            }
        },
    ) { innerPadding ->
        NavHost(
            navController = navController,
            startDestination = MainTab.Dial.route,
            modifier = Modifier.padding(innerPadding),
        ) {
            composable(MainTab.Dial.route) {
                DialScreen(
                    pairing = pairing,
                    manager = manager,
                )
            }
            composable(MainTab.Records.route) {
                CallRecordsScreen(callHistoryModel, navController)
            }
            composable("records/detail/{callId}") { entry ->
                CallRecordDetailScreen(
                    callId = entry.arguments?.getString("callId").orEmpty(),
                    model = callHistoryModel,
                    onBack = { navController.popBackStack() },
                )
            }
            composable(MainTab.Messages.route) {
                MessagesScreen(messageModel, navController)
            }
            composable("messages/detail/{messageId}") { entry ->
                MessageDetailScreen(
                    messageId = entry.arguments?.getString("messageId").orEmpty(),
                    model = messageModel,
                    onBack = { navController.popBackStack() },
                )
            }
            composable(MainTab.Settings.route) {
                SettingsScreen(
                    model = settingsModel,
                    messageState = messageState,
                    callHistoryState = callHistoryState,
                )
            }
        }
    }
}

internal fun isMainTabSelected(tab: MainTab, route: String): Boolean =
    route == tab.route || route.startsWith("${tab.route}/")

internal fun DeviceStatus.toSettingsDeviceStatus(): SettingsDeviceStatus =
    SettingsDeviceStatus(edgeOnline = true, modemOnline = modemOnline)

internal fun HostedDeviceStatus.toSettingsDeviceStatus(): SettingsDeviceStatus =
    SettingsDeviceStatus(edgeOnline = connected, modemOnline = modemOnline)
