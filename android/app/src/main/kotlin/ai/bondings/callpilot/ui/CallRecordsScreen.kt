package ai.bondings.callpilot.ui

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
            CallPageTitle("通话记录")
            UnsupportedContentScreen("当前连接模式暂不支持通话记录同步", Modifier.weight(1f))
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
        CallPageTitle("通话记录")
        PullToRefreshBox(
            isRefreshing = state.isRefreshing,
            onRefresh = { scope.launch { model.refresh() } },
            modifier = Modifier.weight(1f),
        ) {
            when {
                state.records.isNotEmpty() -> LazyColumn(Modifier.fillMaxSize()) {
                    item { CallSyncStatusRow(state.syncStatus, state.errorMessage, state.isRefreshing) }
                    state.errorMessage?.let { error ->
                        item { CallErrorRow(error) }
                    }
                    items(state.records, key = CallRecordItem::callId) { record ->
                        CallRecordRow(record) {
                            navController.navigate("$CALL_DETAIL_ROUTE/${record.callId}")
                        }
                        HorizontalDivider(Modifier.padding(start = 72.dp))
                    }
                    if (state.hasMore) {
                        item {
                            LoadMoreButton(state.isLoadingMore, "加载更多") {
                                scope.launch { model.loadMore() }
                            }
                        }
                    }
                }
                state.syncStatus in setOf(CallHistorySyncStatus.IDLE, CallHistorySyncStatus.LOADING) ->
                    CallCenteredStatus(true, "正在载入通话记录", null)
                state.syncStatus == CallHistorySyncStatus.LIVE ->
                    CallCenteredStatus(false, "暂无通话记录", null)
                else -> CallCenteredStatus(false, "通话记录载入失败", state.errorMessage) {
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
        UnsupportedContentScreen("通话详情不可用")
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
                "通话详情载入失败",
                detailState.errorMessage,
            ) { scope.launch { model.refreshDetail(callId) } }
            else -> CallCenteredStatus(true, "正在载入通话详情", null)
        }
    }
}

@Composable
private fun CallDetailContent(state: CallDetailState, model: CallHistoryModel, callId: String) {
    val detail = requireNotNull(state.detail)
    val scope = rememberCoroutineScope()
    LazyColumn(Modifier.fillMaxSize()) {
        if (state.syncStatus == CallHistorySyncStatus.STALE || state.errorMessage != null) {
            item { CallErrorRow(state.errorMessage ?: "正在显示本机缓存") }
        }
        item { CallMetadata(detail.record) }
        item { CallSummarySection(state) }
        when {
            state.isNormalNoAIContent -> item {
                SectionLabel("AI 内容")
                ListItem(
                    headlineContent = { Text("这通电话由手机端完成，没有 AI 对话或摘要") },
                    leadingContent = { Icon(Icons.Filled.Person, contentDescription = null) },
                )
            }
            state.visibleTimeline.isNotEmpty() -> {
                item { SectionLabel("对话与事件") }
                items(state.visibleTimeline, key = CallTimelineItem::timelineItemId) { item ->
                    TimelineRow(item)
                    HorizontalDivider(Modifier.padding(start = 56.dp))
                }
            }
            state.syncStatus == CallHistorySyncStatus.LOADING -> item {
                ListItem(headlineContent = { Text("正在载入对话") }, leadingContent = { CircularProgressIndicator(Modifier.size(24.dp)) })
            }
            else -> item {
                SectionLabel("对话与事件")
                ListItem(headlineContent = { Text("没有可显示的 AI 对话") })
            }
        }
        if (state.hasMoreTimeline) {
            item {
                LoadMoreButton(state.isLoadingMore, "加载更多对话") {
                    scope.launch { model.loadMoreTimeline(callId) }
                }
            }
        }
    }
}

@Composable
private fun CallRecordRow(record: CallRecordItem, onClick: () -> Unit) {
    val largeText = LocalConfiguration.current.fontScale >= 1.5f
    ListItem(
        modifier = Modifier
            .heightIn(min = 72.dp)
            .clickable(onClick = onClick)
            .semantics { stateDescription = statusLabel(record.status) },
        headlineContent = {
            if (largeText) {
                Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    Text(record.address ?: "未知号码", style = MaterialTheme.typography.titleMedium)
                    Text(formatCallTime(record.startedAt), style = MaterialTheme.typography.labelSmall)
                }
            } else {
                Row(verticalAlignment = Alignment.Top) {
                    Text(record.address ?: "未知号码", Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
                    Text(formatCallTime(record.startedAt), style = MaterialTheme.typography.labelSmall)
                }
            }
        },
        supportingContent = {
            Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text(recordMetadata(record), style = MaterialTheme.typography.bodySmall)
                summaryPreview(record)?.let {
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
    SectionLabel("通话信息")
    CallDetailField(if (record.direction == CallDirection.INBOUND) "来电" else "外呼", record.address ?: "未知号码")
    CallDetailField("开始时间", formatCallTime(record.startedAt))
    record.endedAt?.let { CallDetailField("结束时间", formatCallTime(it)) }
    record.durationMs?.let { CallDetailField("通话时长", durationLabel(it)) }
    CallDetailField("结果", statusLabel(record.status))
    triageOutcomeLabel(record.triageOutcome)?.let { CallDetailField("分诊结果", it) }
}

@Composable
private fun CallSummarySection(state: CallDetailState) {
    val summary = state.detail?.summary
    when (state.summaryPresentation) {
        CallSummaryPresentation.HIDDEN -> Unit
        CallSummaryPresentation.PENDING -> {
            SectionLabel("通话摘要")
            ListItem(
                headlineContent = { Text("摘要生成中") },
                leadingContent = { CircularProgressIndicator(Modifier.size(24.dp)) },
            )
        }
        CallSummaryPresentation.READY -> {
            SectionLabel("通话摘要")
            summary?.text?.takeIf(String::isNotBlank)?.let { value ->
                SelectionContainer { Text(value, Modifier.padding(horizontal = 20.dp, vertical = 12.dp)) }
            }
            summary?.callerIdentity?.takeIf(String::isNotBlank)?.let { CallDetailField("来电人", it) }
            summary?.intent?.takeIf(String::isNotBlank)?.let { CallDetailField("来意", it) }
            summary?.urgency?.takeIf(String::isNotBlank)?.let { CallDetailField("紧急程度", it) }
            summary?.callbackNeeded?.let { CallDetailField("需要回电", if (it) "是" else "否") }
        }
        CallSummaryPresentation.FAILED -> {
            SectionLabel("通话摘要")
            ListItem(
                headlineContent = { Text("摘要生成失败") },
                supportingContent = summary?.errorCode?.takeIf(String::isNotBlank)?.let { code ->
                    { SelectionContainer { Text("错误代码：$code") } }
                },
                leadingContent = { Icon(Icons.Filled.Warning, contentDescription = null, tint = Color(0xFFD97706)) },
            )
        }
    }
}

@Composable
private fun TimelineRow(item: CallTimelineItem) {
    val title = when (item.kind) {
        CallTimelineKind.TRANSCRIPT -> if (item.timelineRole == TimelineRole.CALLER) "对方" else "AI"
        CallTimelineKind.RESULT -> "通话结果"
        CallTimelineKind.TRIAGE -> "智能分诊"
        CallTimelineKind.TAKEOVER -> "真人接管"
        CallTimelineKind.UNKNOWN -> return
    }
    val detail = when (item.kind) {
        CallTimelineKind.TRANSCRIPT -> item.text
        CallTimelineKind.RESULT -> item.summary ?: item.recordStatus?.let(::statusLabel)
        CallTimelineKind.TRIAGE -> listOfNotNull(
            item.triageCategory?.let(::triageCategoryLabel),
            item.triageAction?.let(::triageActionLabel),
        ).joinToString(" · ")
        CallTimelineKind.TAKEOVER -> item.takeoverState?.let(::takeoverStateLabel)
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
            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
        }
        Text("通话详情", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.padding(start = 4.dp))
    }
}

@Composable
private fun CallSyncStatusRow(status: CallHistorySyncStatus, error: String?, refreshing: Boolean) {
    val text = when (status) {
        CallHistorySyncStatus.LIVE -> "已同步"
        CallHistorySyncStatus.STALE -> "正在显示本机缓存"
        CallHistorySyncStatus.OFFLINE -> error ?: "电脑端离线"
        CallHistorySyncStatus.IDLE, CallHistorySyncStatus.LOADING -> "正在同步"
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
        action?.let { OutlinedButton(it, Modifier.padding(top = 16.dp)) { Text("重试") } }
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

private fun recordMetadata(record: CallRecordItem): String = listOfNotNull(
    statusLabel(record.status),
    record.durationMs?.let(::durationLabel),
    sourceLabel(record.source),
).joinToString(" · ")

private fun summaryPreview(record: CallRecordItem): String? = when (record.summaryState) {
    CallSummaryState.PENDING -> "摘要生成中"
    CallSummaryState.READY -> record.summaryPreview ?: "摘要已生成"
    CallSummaryState.FAILED -> "摘要生成失败"
    CallSummaryState.UNAVAILABLE -> null
}

private fun statusLabel(status: CallRecordStatus): String = when (status) {
    CallRecordStatus.COMPLETED -> "已完成"
    CallRecordStatus.NOT_CONNECTED -> "未接通"
    CallRecordStatus.FAILED -> "失败"
    else -> "未知状态"
}

private fun sourceLabel(source: CallSource): String? = when (source) {
    CallSource.AGENT -> "AI 通话"
    CallSource.REMOTE_HANDSET -> "手机通话"
    CallSource.UNKNOWN -> null
}

private fun triageOutcomeLabel(outcome: CallTriageOutcome?): String? = when (outcome) {
    CallTriageOutcome.AI_HANDLED -> "AI 已处理"
    CallTriageOutcome.REJECTED -> "已礼貌拒绝"
    CallTriageOutcome.TRANSFERRED -> "已转接本人"
    CallTriageOutcome.UNKNOWN -> "未知"
    null -> null
}

private fun triageCategoryLabel(value: TriageCategory): String = when (value) {
    TriageCategory.MARKETING -> "营销"
    TriageCategory.PERSONAL -> "个人事务"
    TriageCategory.NEEDS_OWNER -> "需要本人"
    TriageCategory.UNKNOWN -> "未知"
}

private fun triageActionLabel(value: TriageAction): String = when (value) {
    TriageAction.CLARIFY -> "继续确认"
    TriageAction.CONTINUE_AI -> "AI 继续处理"
    TriageAction.REJECT -> "礼貌拒绝"
    TriageAction.TRANSFER -> "转接本人"
}

private fun takeoverStateLabel(value: TakeoverState): String = when (value) {
    TakeoverState.REQUESTED -> "已请求接管"
    TakeoverState.COMMITTED -> "已由本人接管"
    TakeoverState.OWNER_HANGUP -> "本人已挂断"
    TakeoverState.FAILED -> "接管失败"
}

private fun durationLabel(milliseconds: Long): String {
    val seconds = milliseconds / 1_000
    return if (seconds >= 60) "${seconds / 60} 分 ${seconds % 60} 秒" else "$seconds 秒"
}

private fun formatCallTime(epochMs: Long): String =
    DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date(epochMs))

private fun formatCallTimeOnly(epochMs: Long): String =
    DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(epochMs))
