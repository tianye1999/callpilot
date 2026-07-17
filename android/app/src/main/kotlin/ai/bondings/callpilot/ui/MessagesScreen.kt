package ai.bondings.callpilot.ui

import ai.bondings.callpilot.R
import ai.bondings.callpilot.content.MessageInboxModel
import ai.bondings.callpilot.content.MessageSyncStatus
import ai.bondings.callpilot.protocol.MessageDeliveryStatus
import ai.bondings.callpilot.protocol.MessageDirection
import ai.bondings.callpilot.protocol.SMSMessage
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.clickable
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Email
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.LifecycleOwner
import androidx.navigation.NavHostController
import java.text.DateFormat
import java.util.Date
import kotlinx.coroutines.launch
import kotlinx.coroutines.yield

private const val MESSAGE_DETAIL_ROUTE = "messages/detail"

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MessagesScreen(
    model: MessageInboxModel?,
    navController: NavHostController,
) {
    if (model == null) {
        Column(Modifier.fillMaxSize()) {
            PageTitle(stringResource(R.string.messages_title))
            UnsupportedContentScreen(stringResource(R.string.messages_unsupported), Modifier.weight(1f))
        }
        return
    }
    val state by model.state.collectAsState()
    val scope = rememberCoroutineScope()
    val lifecycleOwner = LocalContext.current as? LifecycleOwner

    LaunchedEffect(model) {
        model.setVisible(true)
        model.refresh()
        yield()
        model.markLatestDisplayed()
    }
    DisposableEffect(model, lifecycleOwner) {
        model.setVisible(true)
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) {
                scope.launch {
                    model.refresh()
                    yield()
                    model.markLatestDisplayed()
                }
            }
        }
        lifecycleOwner?.lifecycle?.addObserver(observer)
        onDispose {
            lifecycleOwner?.lifecycle?.removeObserver(observer)
            model.setVisible(false)
        }
    }

    Column(Modifier.fillMaxSize()) {
        PageTitle(stringResource(R.string.messages_title))
        PullToRefreshBox(
            isRefreshing = state.isRefreshing,
            onRefresh = {
                scope.launch {
                    model.refresh()
                    yield()
                    model.markLatestDisplayed()
                }
            },
            modifier = Modifier.weight(1f),
        ) {
            when {
            state.messages.isNotEmpty() -> LazyColumn(Modifier.fillMaxSize()) {
                item { SyncStatusRow(state.syncStatus, state.errorCode, state.isRefreshing) }
                state.errorCode?.let { code ->
                    item {
                        ListItem(
                            headlineContent = { Text(messageErrorText(code)) },
                            leadingContent = {
                                Icon(Icons.Filled.Warning, contentDescription = null, tint = Color(0xFFD97706))
                            },
                        )
                        HorizontalDivider()
                    }
                }
                items(state.messages, key = SMSMessage::messageId) { message ->
                    MessageRow(message) {
                        navController.navigate("$MESSAGE_DETAIL_ROUTE/${message.messageId}")
                    }
                    HorizontalDivider(Modifier.padding(start = 72.dp))
                }
                if (state.hasMore) {
                    item {
                        Box(
                            Modifier
                                .fillMaxWidth()
                                .padding(16.dp),
                            contentAlignment = Alignment.Center,
                        ) {
                            OutlinedButton(
                                onClick = { scope.launch { model.loadMore() } },
                                enabled = !state.isLoadingMore,
                                modifier = Modifier.heightIn(min = 48.dp),
                            ) {
                                if (state.isLoadingMore) CircularProgressIndicator(Modifier.size(24.dp))
                                else Text(stringResource(R.string.common_load_more))
                            }
                        }
                    }
                }
            }
            state.syncStatus in setOf(MessageSyncStatus.IDLE, MessageSyncStatus.LOADING) ->
                CenteredStatus(progress = true, title = stringResource(R.string.messages_loading), detail = null)
            state.syncStatus == MessageSyncStatus.LIVE ->
                CenteredStatus(progress = false, title = stringResource(R.string.messages_empty), detail = null)
            else -> CenteredStatus(
                progress = false,
                title = stringResource(R.string.messages_load_failed),
                detail = messageErrorText(state.errorCode),
                action = { scope.launch { model.refresh() } },
            )
            }
        }
    }
}

@Composable
fun MessageDetailScreen(
    messageId: String,
    model: MessageInboxModel?,
    onBack: () -> Unit,
) {
    if (model == null) {
        UnsupportedContentScreen(stringResource(R.string.messages_detail_unavailable))
        return
    }
    val state by model.state.collectAsState()
    val message = state.messages.firstOrNull { it.messageId == messageId }
        ?: return UnsupportedContentScreen(stringResource(R.string.messages_detail_unavailable))
    Column(Modifier.fillMaxSize()) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 8.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            IconButton(onClick = onBack) {
                Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = stringResource(R.string.common_back))
            }
            Text(
                stringResource(R.string.messages_detail_title),
                style = MaterialTheme.typography.headlineSmall,
                modifier = Modifier.padding(start = 4.dp),
            )
        }
        LazyColumn(Modifier.weight(1f)) {
            item {
                DetailField(
                    if (message.direction == MessageDirection.INBOUND) {
                        stringResource(R.string.messages_detail_sender)
                    } else {
                        stringResource(R.string.messages_detail_recipient)
                    },
                    message.address,
                )
                DetailField(stringResource(R.string.messages_detail_time), formatMessageTime(message.occurredAt))
                DetailField(stringResource(R.string.messages_detail_status), deliveryLabel(message))
                HorizontalDivider()
                SelectionContainer {
                    Text(
                        message.text,
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(20.dp),
                        style = MaterialTheme.typography.bodyLarge,
                    )
                }
            }
        }
    }
}

@Composable
private fun MessageRow(message: SMSMessage, onClick: () -> Unit) {
    val largeText = LocalConfiguration.current.fontScale >= 1.5f
    val localizedDelivery = deliveryLabel(message)
    ListItem(
        modifier = Modifier
            .heightIn(min = 64.dp)
            .clickable(onClick = onClick)
            .semantics { stateDescription = localizedDelivery },
        headlineContent = {
            if (largeText) {
                Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    Text(message.address, style = MaterialTheme.typography.titleMedium)
                    Text(formatMessageTime(message.occurredAt), style = MaterialTheme.typography.labelSmall)
                }
            } else {
                Row(verticalAlignment = Alignment.Top) {
                    Text(message.address, modifier = Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
                    Text(formatMessageTime(message.occurredAt), style = MaterialTheme.typography.labelSmall)
                }
            }
        },
        supportingContent = {
            Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text(message.text, maxLines = 2, style = MaterialTheme.typography.bodyMedium)
                Text(localizedDelivery, style = MaterialTheme.typography.labelSmall)
            }
        },
        leadingContent = {
            Icon(
                Icons.Filled.Email,
                contentDescription = null,
                tint = if (message.direction == MessageDirection.INBOUND) Color(0xFF16803A) else MaterialTheme.colorScheme.primary,
            )
        },
    )
}

@Composable
private fun SyncStatusRow(status: MessageSyncStatus, errorCode: String?, refreshing: Boolean) {
    val text = when (status) {
        MessageSyncStatus.LIVE -> stringResource(R.string.common_synced)
        MessageSyncStatus.STALE -> stringResource(R.string.common_stale_cache)
        MessageSyncStatus.OFFLINE -> messageErrorText(errorCode)
        MessageSyncStatus.IDLE, MessageSyncStatus.LOADING -> stringResource(R.string.common_syncing)
    }
    ListItem(
        headlineContent = { Text(text) },
        trailingContent = { if (refreshing) CircularProgressIndicator() },
    )
}

@Composable
private fun CenteredStatus(
    progress: Boolean,
    title: String,
    detail: String?,
    action: (() -> Unit)? = null,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        if (progress) CircularProgressIndicator()
        Text(title, style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(top = 12.dp))
        detail?.let { Text(it, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(top = 8.dp)) }
        action?.let {
            OutlinedButton(onClick = it, modifier = Modifier.padding(top = 16.dp)) {
                Text(stringResource(R.string.common_retry))
            }
        }
    }
}

@Composable
private fun DetailField(label: String, value: String) {
    Column(Modifier.padding(horizontal = 20.dp, vertical = 12.dp)) {
        Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, style = MaterialTheme.typography.bodyLarge)
    }
}

@Composable
private fun PageTitle(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.headlineSmall,
        modifier = Modifier.padding(horizontal = 20.dp, vertical = 16.dp),
    )
}

@Composable
internal fun UnsupportedContentScreen(message: String, modifier: Modifier = Modifier) {
    Box(modifier) {
        CenteredStatus(progress = false, title = message, detail = null)
    }
}

@Composable
private fun deliveryLabel(message: SMSMessage): String = when (message.status) {
    MessageDeliveryStatus.RECEIVED -> stringResource(R.string.messages_status_received)
    MessageDeliveryStatus.SENT -> stringResource(R.string.messages_status_sent)
    MessageDeliveryStatus.FAILED -> stringResource(R.string.messages_status_failed)
    MessageDeliveryStatus.ERROR -> stringResource(R.string.messages_status_error)
}

@Composable
private fun messageErrorText(code: String?): String = when (code) {
    "PAYLOAD_TOO_LARGE" -> stringResource(R.string.messages_error_payload_too_large)
    "EDGE_OFFLINE", "TIMEOUT" -> stringResource(R.string.messages_error_edge_offline)
    "FEATURE_DISABLED", "FORBIDDEN" -> stringResource(R.string.messages_error_feature_disabled)
    "UNAUTHORIZED" -> stringResource(R.string.content_error_unauthorized)
    else -> stringResource(R.string.messages_error_unavailable)
}

private fun formatMessageTime(epochMs: Long): String =
    DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date(epochMs))
