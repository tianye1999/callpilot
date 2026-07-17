package ai.bondings.callpilot.ui

import ai.bondings.callpilot.call.CallManager
import ai.bondings.callpilot.content.MessageCacheStore
import ai.bondings.callpilot.content.MessageInboxState
import ai.bondings.callpilot.content.MessageInboxModel
import ai.bondings.callpilot.content.ProtectedJsonStore
import ai.bondings.callpilot.pairing.CredentialStore
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.HostedCloudClient
import ai.bondings.callpilot.protocol.PairingProtocol
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Email
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import kotlinx.coroutines.flow.MutableStateFlow

enum class MainTab(
    val route: String,
    val label: String,
    val icon: ImageVector,
) {
    Dial("dial", "拨号", Icons.Filled.Call),
    Records("records", "记录", Icons.Filled.DateRange),
    Messages("messages", "短信", Icons.Filled.Email),
    Settings("settings", "设置", Icons.Filled.Settings),
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
    val messageModel = remember(pairing) {
        if (pairing.protocol == PairingProtocol.HOSTED) {
            val client = HostedCloudClient(pairing.gatewayUrl).also {
                it.credential = pairing.credential
            }
            MessageInboxModel(
                client = client,
                store = MessageCacheStore(ProtectedJsonStore.messages(context)),
                deviceId = pairing.credential.deviceId,
                onUnauthorized = {
                    store.clear()
                    onUnpaired()
                },
            )
        } else {
            null
        }
    }
    val emptyMessageState = remember { MutableStateFlow(MessageInboxState()) }
    val messageState by (messageModel?.state ?: emptyMessageState).collectAsState()
    LaunchedEffect(messageModel) {
        messageModel?.loadCachedContent()
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
                        label = { Text(tab.label) },
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
                    store = store,
                    manager = manager,
                    onUnpaired = onUnpaired,
                )
            }
            composable(MainTab.Records.route) {
                PendingContentScreen("通话记录", "完整记录将在下一批接入")
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
                PendingContentScreen("设置", "设备与隐私设置将在下一批接入")
            }
        }
    }
}

internal fun isMainTabSelected(tab: MainTab, route: String): Boolean =
    route == tab.route || route.startsWith("${tab.route}/")

@Composable
private fun PendingContentScreen(title: String, message: String) {
    Box(
        modifier = Modifier.fillMaxSize(),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = "$title\n$message",
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
