package ai.bondings.callpilot.ui

import ai.bondings.callpilot.R
import ai.bondings.callpilot.content.CallHistoryState
import ai.bondings.callpilot.content.CallHistorySyncStatus
import ai.bondings.callpilot.content.MessageInboxState
import ai.bondings.callpilot.content.MessageSyncStatus
import ai.bondings.callpilot.content.SettingsModel
import ai.bondings.callpilot.content.SettingsSyncStatus
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ExitToApp
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch

@Composable
fun SettingsScreen(
    model: SettingsModel,
    messageState: MessageInboxState,
    callHistoryState: CallHistoryState,
) {
    val state by model.state.collectAsState()
    val scope = rememberCoroutineScope()
    var confirmingClear by remember { mutableStateOf(false) }
    var confirmingUnpair by remember { mutableStateOf(false) }

    LaunchedEffect(model) { model.refresh() }

    LazyColumn {
        item { SettingsPageTitle() }
        item { SettingsSectionLabel(stringResource(R.string.settings_connection_section)) }
        item {
            SettingsStatusRow(
                title = stringResource(R.string.settings_connection_edge),
                online = state.deviceStatus?.edgeOnline,
            )
        }
        item {
            SettingsStatusRow(
                title = stringResource(R.string.settings_connection_sim),
                online = state.deviceStatus?.modemOnline,
            )
        }
        item {
            ListItem(
                headlineContent = { Text(settingsSyncLabel(state.syncStatus)) },
                trailingContent = {
                    if (state.isRefreshing) CircularProgressIndicator(Modifier.size(24.dp))
                },
            )
        }
        item {
            OutlinedButton(
                onClick = { scope.launch { model.refresh() } },
                enabled = !state.isRefreshing,
                modifier = Modifier.padding(horizontal = 20.dp).heightIn(min = 48.dp),
            ) {
                Icon(Icons.Filled.Refresh, contentDescription = null)
                Text(stringResource(R.string.settings_connection_refresh), Modifier.padding(start = 8.dp))
            }
        }

        item { SettingsSectionLabel(stringResource(R.string.settings_cache_section)) }
        item {
            SettingsCacheRow(
                title = stringResource(R.string.settings_cache_messages),
                count = stringResource(R.string.settings_cache_messages_count, messageState.messages.size),
                status = messageCacheStatus(messageState.syncStatus),
            )
        }
        item {
            SettingsCacheRow(
                title = stringResource(R.string.settings_cache_calls),
                count = stringResource(R.string.settings_cache_calls_count, callHistoryState.records.size),
                status = callCacheStatus(callHistoryState.syncStatus),
            )
        }
        item {
            Button(
                onClick = { confirmingClear = true },
                enabled = !state.isClearingContent && !state.isUnpairing,
                modifier = Modifier.padding(horizontal = 20.dp, vertical = 8.dp).heightIn(min = 48.dp),
            ) {
                if (state.isClearingContent) {
                    CircularProgressIndicator(Modifier.size(20.dp))
                } else {
                    Icon(Icons.Filled.Delete, contentDescription = null)
                }
                Text(stringResource(R.string.settings_cache_clear), Modifier.padding(start = 8.dp))
            }
        }
        item {
            Text(
                stringResource(R.string.settings_cache_footer),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(horizontal = 20.dp, vertical = 8.dp),
            )
        }

        item { SettingsSectionLabel(stringResource(R.string.settings_privacy_section)) }
        item {
            Column(
                Modifier.padding(horizontal = 20.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Text(stringResource(R.string.settings_privacy_relay), style = MaterialTheme.typography.bodyMedium)
                Text(stringResource(R.string.settings_privacy_local), style = MaterialTheme.typography.bodyMedium)
            }
        }
        item {
            TextButton(
                onClick = { confirmingUnpair = true },
                enabled = !state.isUnpairing && !state.isClearingContent,
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 16.dp).heightIn(min = 48.dp),
            ) {
                if (state.isUnpairing) {
                    CircularProgressIndicator(Modifier.size(20.dp))
                } else {
                    Icon(Icons.AutoMirrored.Filled.ExitToApp, contentDescription = null)
                }
                Text(
                    stringResource(R.string.settings_unpair_action),
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(start = 8.dp),
                )
            }
        }
    }

    if (confirmingClear) {
        AlertDialog(
            onDismissRequest = { confirmingClear = false },
            title = { Text(stringResource(R.string.settings_cache_clear_confirm_title)) },
            text = { Text(stringResource(R.string.settings_cache_clear_confirm_message)) },
            confirmButton = {
                TextButton(onClick = {
                    confirmingClear = false
                    scope.launch { model.clearLocalContent() }
                }) { Text(stringResource(R.string.settings_cache_clear_confirm_action)) }
            },
            dismissButton = {
                TextButton(onClick = { confirmingClear = false }) {
                    Text(stringResource(R.string.common_cancel))
                }
            },
        )
    }

    if (confirmingUnpair) {
        AlertDialog(
            onDismissRequest = { confirmingUnpair = false },
            title = { Text(stringResource(R.string.settings_unpair_confirm_title)) },
            text = { Text(stringResource(R.string.settings_unpair_confirm_message)) },
            confirmButton = {
                TextButton(onClick = {
                    confirmingUnpair = false
                    scope.launch { model.unpair() }
                }) { Text(stringResource(R.string.settings_unpair_action)) }
            },
            dismissButton = {
                TextButton(onClick = { confirmingUnpair = false }) {
                    Text(stringResource(R.string.common_cancel))
                }
            },
        )
    }
}

@Composable
private fun SettingsPageTitle() {
    Text(
        stringResource(R.string.settings_title),
        style = MaterialTheme.typography.headlineSmall,
        modifier = Modifier.padding(horizontal = 20.dp, vertical = 16.dp),
    )
}

@Composable
private fun SettingsSectionLabel(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.titleMedium,
        color = MaterialTheme.colorScheme.primary,
        modifier = Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 12.dp),
    )
}

@Composable
private fun SettingsStatusRow(title: String, online: Boolean?) {
    val label = when (online) {
        true -> stringResource(R.string.settings_status_online)
        false -> stringResource(R.string.settings_status_offline)
        null -> stringResource(R.string.settings_status_checking)
    }
    val color = when (online) {
        true -> Color(0xFF16803A)
        false -> MaterialTheme.colorScheme.error
        null -> MaterialTheme.colorScheme.onSurfaceVariant
    }
    ListItem(
        headlineContent = { Text(title) },
        trailingContent = { Text(label, color = color) },
    )
    HorizontalDivider()
}

@Composable
private fun SettingsCacheRow(title: String, count: String, status: String) {
    ListItem(
        headlineContent = { Text(title) },
        supportingContent = { Text(status) },
        trailingContent = { Text(count) },
    )
    HorizontalDivider()
}

@Composable
private fun settingsSyncLabel(status: SettingsSyncStatus): String = when (status) {
    SettingsSyncStatus.IDLE -> stringResource(R.string.settings_sync_idle)
    SettingsSyncStatus.LOADING -> stringResource(R.string.settings_sync_loading)
    SettingsSyncStatus.LIVE -> stringResource(R.string.settings_sync_live)
    SettingsSyncStatus.STALE -> stringResource(R.string.settings_sync_stale)
    SettingsSyncStatus.OFFLINE -> stringResource(R.string.settings_sync_offline)
}

@Composable
private fun messageCacheStatus(status: MessageSyncStatus): String = when (status) {
    MessageSyncStatus.IDLE -> stringResource(R.string.settings_cache_empty)
    MessageSyncStatus.LOADING -> stringResource(R.string.settings_cache_loading)
    MessageSyncStatus.LIVE -> stringResource(R.string.settings_cache_live)
    MessageSyncStatus.STALE -> stringResource(R.string.settings_cache_stale)
    MessageSyncStatus.OFFLINE -> stringResource(R.string.settings_cache_offline)
}

@Composable
private fun callCacheStatus(status: CallHistorySyncStatus): String = when (status) {
    CallHistorySyncStatus.IDLE -> stringResource(R.string.settings_cache_empty)
    CallHistorySyncStatus.LOADING -> stringResource(R.string.settings_cache_loading)
    CallHistorySyncStatus.LIVE -> stringResource(R.string.settings_cache_live)
    CallHistorySyncStatus.STALE -> stringResource(R.string.settings_cache_stale)
    CallHistorySyncStatus.OFFLINE -> stringResource(R.string.settings_cache_offline)
}
