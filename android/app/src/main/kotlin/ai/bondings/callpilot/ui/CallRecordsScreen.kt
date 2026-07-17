package ai.bondings.callpilot.ui

import ai.bondings.callpilot.R
import ai.bondings.callpilot.content.CallDetailState
import ai.bondings.callpilot.content.CallHistoryModel
import ai.bondings.callpilot.content.CallHistorySyncStatus
import ai.bondings.callpilot.content.CallSummaryPresentation
import ai.bondings.callpilot.protocol.CallDirection
import ai.bondings.callpilot.protocol.CallRecordItem
import ai.bondings.callpilot.protocol.CallRecordStatus
import ai.bondings.callpilot.protocol.CallSource
import ai.bondings.callpilot.protocol.CallSummaryState
import ai.bondings.callpilot.protocol.CallTimelineItem
import ai.bondings.callpilot.protocol.CallTimelineKind
import ai.bondings.callpilot.protocol.CallTriageOutcome
import ai.bondings.callpilot.protocol.TakeoverState
import ai.bondings.callpilot.protocol.TimelineRole
import ai.bondings.callpilot.protocol.TriageAction
import ai.bondings.callpilot.protocol.TriageCategory
import androidx.compose.foundation.clickable
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
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.Person
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
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
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

private const val CALL_DETAIL_ROUTE = "records/detail"

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CallRecordsScreen(
    model: CallHistoryModel?,
    navController: NavHostController,
) {
    if (model == null) {
        Column(Modifier.fillMaxSize()) {
            CallPageTitle(stringResource(R.string.calls_title))
            UnsupportedContentScreen(stringResource(R.string.calls_unsupported), Modifier.weight(1f))
        }
        return
    }
    val state by model.state.collectAsState()
    val scope = rememberCoroutineScope()
    val lifecycleOwner = LocalContext.current as? LifecycleOwner

    LaunchedEffect(model) { model.refresh() }
    DisposableEffect(model, lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) scope.launch { model.refresh() }
        }
        lifecycleOwner?.lifecycle?.addObserver(observer)
        onDispose { lifecycleOwner?.lifecycle?.removeObserver(observer) }
    }

    Column(Modifier.fillMaxSize()) {
        CallPageTitle(stringResource(R.string.calls_title))
        PullToRefreshBox(
            isRefreshing = state.isRefreshing,
            onRefresh = { scope.launch { model.refresh() } },
            modifier = Modifier.weight(1f),
        ) {
            when {
                state.records.isNotEmpty() -> LazyColumn(Modifier.fillMaxSize()) {
                    item { CallSyncStatusRow(state.syncStatus, state.errorCode, state.isRefreshing) }
                    state.errorCode?.let { code ->
                        item { CallErrorRow(callErrorText(code)) }
                    }
                    items(state.records, key = CallRecordItem::callId) { record ->
                        CallRecordRow(record) {
                            navController.navigate("$CALL_DETAIL_ROUTE/${record.callId}")
                        }
                        HorizontalDivider(Modifier.padding(start = 72.dp))
                    }
                    if (state.hasMore) {
                        item {
                            LoadMoreButton(state.isLoadingMore, stringResource(R.string.common_load_more)) {
                                scope.launch { model.loadMore() }
                            }
                        }
                    }
                }
                state.syncStatus in setOf(CallHistorySyncStatus.IDLE, CallHistorySyncStatus.LOADING) ->
                    CallCenteredStatus(true, stringResource(R.string.calls_loading), null)
                state.syncStatus == CallHistorySyncStatus.LIVE ->
                    CallCenteredStatus(false, stringResource(R.string.calls_empty), null)
                else -> CallCenteredStatus(
                    false,
                    stringResource(R.string.calls_load_failed),
                    callErrorText(state.errorCode),
                ) {
                    scope.launch { model.refresh() }
                }
            }
        }
    }
}

@Composable
fun CallRecordDetailScreen(
    callId: String,
    model: CallHistoryModel?,
    onBack: () -> Unit,
) {
    if (model == null) {
        UnsupportedContentScreen(stringResource(R.string.calls_detail_load_failed))
        return
    }
    val state by model.state.collectAsState()
    val detailState = state.details[callId]
    val scope = rememberCoroutineScope()
    val lifecycleOwner = LocalContext.current as? LifecycleOwner

    LaunchedEffect(model, callId) { model.refreshDetail(callId) }
    DisposableEffect(model, callId, lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) scope.launch { model.refreshDetail(callId) }
        }
        lifecycleOwner?.lifecycle?.addObserver(observer)
        onDispose { lifecycleOwner?.lifecycle?.removeObserver(observer) }
    }

    Column(Modifier.fillMaxSize()) {
        CallDetailTitle(onBack)
        when {
            detailState?.detail != null -> CallDetailContent(detailState, model, callId)
            detailState?.syncStatus == CallHistorySyncStatus.OFFLINE -> CallCenteredStatus(
                false,
                stringResource(R.string.calls_detail_load_failed),
                callErrorText(detailState.errorCode),
            ) { scope.launch { model.refreshDetail(callId) } }
            else -> CallCenteredStatus(true, stringResource(R.string.calls_detail_loading), null)
        }
    }
}

@Composable
private fun CallDetailContent(state: CallDetailState, model: CallHistoryModel, callId: String) {
    val detail = requireNotNull(state.detail)
    val scope = rememberCoroutineScope()
    LazyColumn(Modifier.fillMaxSize()) {
        if (state.syncStatus == CallHistorySyncStatus.STALE || state.errorCode != null) {
            item {
                CallErrorRow(
                    state.errorCode?.let { callErrorText(it) }
                        ?: stringResource(R.string.common_stale_cache),
                )
            }
        }
        item { CallMetadata(detail.record) }
        item { CallSummarySection(state) }
        when {
            state.isNormalNoAIContent -> item {
                SectionLabel(stringResource(R.string.calls_ai_section))
                ListItem(
                    headlineContent = { Text(stringResource(R.string.calls_ai_no_content)) },
                    leadingContent = { Icon(Icons.Filled.Person, contentDescription = null) },
                )
            }
            state.visibleTimeline.isNotEmpty() -> {
                item { SectionLabel(stringResource(R.string.calls_timeline_section)) }
                items(state.visibleTimeline, key = CallTimelineItem::timelineItemId) { item ->
                    TimelineRow(item)
                    HorizontalDivider(Modifier.padding(start = 56.dp))
                }
            }
            state.syncStatus == CallHistorySyncStatus.LOADING -> item {
                ListItem(
                    headlineContent = { Text(stringResource(R.string.calls_timeline_loading)) },
                    leadingContent = { CircularProgressIndicator(Modifier.size(24.dp)) },
                )
            }
            else -> item {
                SectionLabel(stringResource(R.string.calls_timeline_section))
                ListItem(headlineContent = { Text(stringResource(R.string.calls_timeline_empty)) })
            }
        }
        if (state.hasMoreTimeline) {
            item {
                LoadMoreButton(state.isLoadingMore, stringResource(R.string.calls_timeline_load_more)) {
                    scope.launch { model.loadMoreTimeline(callId) }
                }
            }
        }
    }
}

@Composable
private fun CallRecordRow(record: CallRecordItem, onClick: () -> Unit) {
    val largeText = LocalConfiguration.current.fontScale >= 1.5f
    val address = record.address ?: stringResource(R.string.calls_unknown_address)
    val localizedStatus = statusLabel(record.status)
    val metadata = recordMetadata(record)
    val summary = summaryPreview(record)
    ListItem(
        modifier = Modifier
            .heightIn(min = 72.dp)
            .clickable(onClick = onClick)
            .semantics { stateDescription = localizedStatus },
        headlineContent = {
            if (largeText) {
                Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    Text(address, style = MaterialTheme.typography.titleMedium)
                    Text(formatCallTime(record.startedAt), style = MaterialTheme.typography.labelSmall)
                }
            } else {
                Row(verticalAlignment = Alignment.Top) {
                    Text(address, Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
                    Text(formatCallTime(record.startedAt), style = MaterialTheme.typography.labelSmall)
                }
            }
        },
        supportingContent = {
            Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text(metadata, style = MaterialTheme.typography.bodySmall)
                summary?.let {
                    Text(
                        it,
                        maxLines = 2,
                        style = MaterialTheme.typography.bodyMedium,
                        color = if (record.summaryState == CallSummaryState.FAILED) Color(0xFFD97706) else MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        },
        leadingContent = {
            Icon(
                Icons.Filled.Call,
                contentDescription = null,
                tint = if (record.direction == CallDirection.INBOUND) Color(0xFF16803A) else MaterialTheme.colorScheme.primary,
            )
        },
    )
}

@Composable
private fun CallMetadata(record: CallRecordItem) {
    SectionLabel(stringResource(R.string.calls_metadata_section))
    CallDetailField(
        if (record.direction == CallDirection.INBOUND) {
            stringResource(R.string.calls_direction_inbound)
        } else {
            stringResource(R.string.calls_direction_outbound)
        },
        record.address ?: stringResource(R.string.calls_unknown_address),
    )
    CallDetailField(stringResource(R.string.calls_metadata_started_at), formatCallTime(record.startedAt))
    record.endedAt?.let {
        CallDetailField(stringResource(R.string.calls_metadata_ended_at), formatCallTime(it))
    }
    record.durationMs?.let {
        CallDetailField(stringResource(R.string.calls_metadata_duration), durationLabel(it))
    }
    CallDetailField(stringResource(R.string.calls_metadata_result), statusLabel(record.status))
    triageOutcomeLabel(record.triageOutcome)?.let {
        CallDetailField(stringResource(R.string.calls_metadata_triage), it)
    }
}

@Composable
private fun CallSummarySection(state: CallDetailState) {
    val summary = state.detail?.summary
    when (state.summaryPresentation) {
        CallSummaryPresentation.HIDDEN -> Unit
        CallSummaryPresentation.PENDING -> {
            SectionLabel(stringResource(R.string.calls_summary_section))
            ListItem(
                headlineContent = { Text(stringResource(R.string.calls_summary_loading)) },
                leadingContent = { CircularProgressIndicator(Modifier.size(24.dp)) },
            )
        }
        CallSummaryPresentation.READY -> {
            SectionLabel(stringResource(R.string.calls_summary_section))
            summary?.text?.takeIf(String::isNotBlank)?.let { value ->
                SelectionContainer { Text(value, Modifier.padding(horizontal = 20.dp, vertical = 12.dp)) }
            }
            summary?.callerIdentity?.takeIf(String::isNotBlank)?.let {
                CallDetailField(stringResource(R.string.calls_summary_caller), it)
            }
            summary?.intent?.takeIf(String::isNotBlank)?.let {
                CallDetailField(stringResource(R.string.calls_summary_intent), it)
            }
            summary?.urgency?.takeIf(String::isNotBlank)?.let {
                CallDetailField(stringResource(R.string.calls_summary_urgency), it)
            }
            summary?.callbackNeeded?.let {
                CallDetailField(
                    stringResource(R.string.calls_summary_callback),
                    stringResource(if (it) R.string.common_yes else R.string.common_no),
                )
            }
        }
        CallSummaryPresentation.FAILED -> {
            SectionLabel(stringResource(R.string.calls_summary_section))
            ListItem(
                headlineContent = { Text(stringResource(R.string.calls_summary_failed)) },
                supportingContent = summary?.errorCode?.takeIf(String::isNotBlank)?.let { code ->
                    { SelectionContainer { Text(stringResource(R.string.calls_summary_error_code, code)) } }
                },
                leadingContent = { Icon(Icons.Filled.Warning, contentDescription = null, tint = Color(0xFFD97706)) },
            )
        }
    }
}

@Composable
private fun TimelineRow(item: CallTimelineItem) {
    val resultStatus = item.recordStatus?.let { statusLabel(it) }
    val triageCategory = item.triageCategory?.let { triageCategoryLabel(it) }
    val triageAction = item.triageAction?.let { triageActionLabel(it) }
    val takeoverState = item.takeoverState?.let { takeoverStateLabel(it) }
    val title = when (item.kind) {
        CallTimelineKind.TRANSCRIPT -> if (item.timelineRole == TimelineRole.CALLER) {
            stringResource(R.string.calls_timeline_caller)
        } else {
            stringResource(R.string.calls_timeline_ai)
        }
        CallTimelineKind.RESULT -> stringResource(R.string.calls_timeline_result)
        CallTimelineKind.TRIAGE -> stringResource(R.string.calls_timeline_triage)
        CallTimelineKind.TAKEOVER -> stringResource(R.string.calls_timeline_takeover)
        CallTimelineKind.UNKNOWN -> return
    }
    val detail = when (item.kind) {
        CallTimelineKind.TRANSCRIPT -> item.text
        CallTimelineKind.RESULT -> item.summary ?: resultStatus
        CallTimelineKind.TRIAGE -> listOfNotNull(
            triageCategory,
            triageAction,
        ).joinToString(" · ")
        CallTimelineKind.TAKEOVER -> takeoverState
        CallTimelineKind.UNKNOWN -> null
    }
    ListItem(
        headlineContent = {
            Row(verticalAlignment = Alignment.Top) {
                Text(title, Modifier.weight(1f), style = MaterialTheme.typography.titleSmall)
                Text(formatCallTimeOnly(item.occurredAt), style = MaterialTheme.typography.labelSmall)
            }
        },
        supportingContent = detail?.let { value ->
            { SelectionContainer { Text(value) } }
        },
        leadingContent = { Icon(Icons.Filled.Person, contentDescription = null) },
    )
}

@Composable
private fun CallDetailTitle(onBack: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        IconButton(onClick = onBack) {
            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = stringResource(R.string.common_back))
        }
        Text(
            stringResource(R.string.calls_detail_title),
            style = MaterialTheme.typography.headlineSmall,
            modifier = Modifier.padding(start = 4.dp),
        )
    }
}

@Composable
private fun CallSyncStatusRow(status: CallHistorySyncStatus, errorCode: String?, refreshing: Boolean) {
    val text = when (status) {
        CallHistorySyncStatus.LIVE -> stringResource(R.string.common_synced)
        CallHistorySyncStatus.STALE -> stringResource(R.string.common_stale_cache)
        CallHistorySyncStatus.OFFLINE -> callErrorText(errorCode)
        CallHistorySyncStatus.IDLE, CallHistorySyncStatus.LOADING -> stringResource(R.string.common_syncing)
    }
    ListItem(
        headlineContent = { Text(text) },
        trailingContent = { if (refreshing) CircularProgressIndicator(Modifier.size(24.dp)) },
    )
}

@Composable
private fun CallErrorRow(message: String) {
    ListItem(
        headlineContent = { Text(message) },
        leadingContent = { Icon(Icons.Filled.Warning, contentDescription = null, tint = Color(0xFFD97706)) },
    )
    HorizontalDivider()
}

@Composable
private fun LoadMoreButton(loading: Boolean, label: String, onClick: () -> Unit) {
    Box(
        Modifier.fillMaxWidth().padding(16.dp),
        contentAlignment = Alignment.Center,
    ) {
        OutlinedButton(onClick = onClick, enabled = !loading, modifier = Modifier.heightIn(min = 48.dp)) {
            if (loading) CircularProgressIndicator(Modifier.size(24.dp)) else Text(label)
        }
    }
}

@Composable
private fun CallCenteredStatus(progress: Boolean, title: String, detail: String?, action: (() -> Unit)? = null) {
    Column(
        Modifier.fillMaxSize().padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        if (progress) CircularProgressIndicator()
        Text(title, style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(top = 12.dp))
        detail?.let { Text(it, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(top = 8.dp)) }
        action?.let {
            OutlinedButton(it, Modifier.padding(top = 16.dp)) {
                Text(stringResource(R.string.common_retry))
            }
        }
    }
}

@Composable
private fun SectionLabel(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.titleMedium,
        color = MaterialTheme.colorScheme.primary,
        modifier = Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 12.dp),
    )
}

@Composable
private fun CallDetailField(label: String, value: String) {
    Column(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 8.dp)) {
        Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        SelectionContainer { Text(value, style = MaterialTheme.typography.bodyLarge) }
    }
}

@Composable
private fun CallPageTitle(text: String) {
    Text(text, style = MaterialTheme.typography.headlineSmall, modifier = Modifier.padding(horizontal = 20.dp, vertical = 16.dp))
}

@Composable
private fun recordMetadata(record: CallRecordItem): String {
    val duration = record.durationMs?.let { durationLabel(it) }
    return listOfNotNull(statusLabel(record.status), duration, sourceLabel(record.source)).joinToString(" · ")
}

@Composable
private fun summaryPreview(record: CallRecordItem): String? = when (record.summaryState) {
    CallSummaryState.PENDING -> stringResource(R.string.calls_summary_pending_preview)
    CallSummaryState.READY -> record.summaryPreview ?: stringResource(R.string.calls_summary_ready_preview)
    CallSummaryState.FAILED -> stringResource(R.string.calls_summary_failed_preview)
    CallSummaryState.UNAVAILABLE -> null
}

@Composable
private fun statusLabel(status: CallRecordStatus): String = when (status) {
    CallRecordStatus.COMPLETED -> stringResource(R.string.calls_status_completed)
    CallRecordStatus.NOT_CONNECTED -> stringResource(R.string.calls_status_not_connected)
    CallRecordStatus.FAILED -> stringResource(R.string.calls_status_failed)
    else -> stringResource(R.string.calls_status_unknown)
}

@Composable
private fun sourceLabel(source: CallSource): String? = when (source) {
    CallSource.AGENT -> stringResource(R.string.calls_source_agent)
    CallSource.REMOTE_HANDSET -> stringResource(R.string.calls_source_remote_handset)
    CallSource.UNKNOWN -> null
}

@Composable
private fun triageOutcomeLabel(outcome: CallTriageOutcome?): String? = when (outcome) {
    CallTriageOutcome.AI_HANDLED -> stringResource(R.string.calls_triage_outcome_ai_handled)
    CallTriageOutcome.REJECTED -> stringResource(R.string.calls_triage_outcome_rejected)
    CallTriageOutcome.TRANSFERRED -> stringResource(R.string.calls_triage_outcome_transferred)
    CallTriageOutcome.UNKNOWN -> stringResource(R.string.calls_triage_outcome_unknown)
    null -> null
}

@Composable
private fun triageCategoryLabel(value: TriageCategory): String = when (value) {
    TriageCategory.MARKETING -> stringResource(R.string.calls_triage_category_marketing)
    TriageCategory.PERSONAL -> stringResource(R.string.calls_triage_category_personal)
    TriageCategory.NEEDS_OWNER -> stringResource(R.string.calls_triage_category_needs_owner)
    TriageCategory.UNKNOWN -> stringResource(R.string.calls_triage_category_unknown)
}

@Composable
private fun triageActionLabel(value: TriageAction): String = when (value) {
    TriageAction.CLARIFY -> stringResource(R.string.calls_triage_action_clarify)
    TriageAction.CONTINUE_AI -> stringResource(R.string.calls_triage_action_continue_ai)
    TriageAction.REJECT -> stringResource(R.string.calls_triage_action_reject)
    TriageAction.TRANSFER -> stringResource(R.string.calls_triage_action_transfer)
}

@Composable
private fun takeoverStateLabel(value: TakeoverState): String = when (value) {
    TakeoverState.REQUESTED -> stringResource(R.string.calls_takeover_requested)
    TakeoverState.COMMITTED -> stringResource(R.string.calls_takeover_committed)
    TakeoverState.OWNER_HANGUP -> stringResource(R.string.calls_takeover_owner_hangup)
    TakeoverState.FAILED -> stringResource(R.string.calls_takeover_failed)
}

@Composable
private fun durationLabel(milliseconds: Long): String {
    val seconds = milliseconds / 1_000
    return if (seconds >= 60) {
        stringResource(R.string.calls_duration_minutes_seconds, seconds / 60, seconds % 60)
    } else {
        stringResource(R.string.calls_duration_seconds, seconds)
    }
}

@Composable
private fun callErrorText(code: String?): String = when (code) {
    "PAYLOAD_TOO_LARGE" -> stringResource(R.string.calls_error_payload_too_large)
    "EDGE_OFFLINE", "TIMEOUT" -> stringResource(R.string.calls_error_edge_offline)
    "FEATURE_DISABLED", "FORBIDDEN" -> stringResource(R.string.calls_error_feature_disabled)
    "UNAUTHORIZED" -> stringResource(R.string.content_error_unauthorized)
    else -> stringResource(R.string.calls_error_unavailable)
}

private fun formatCallTime(epochMs: Long): String =
    DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date(epochMs))

private fun formatCallTimeOnly(epochMs: Long): String =
    DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(epochMs))
