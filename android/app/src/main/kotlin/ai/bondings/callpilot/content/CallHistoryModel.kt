package ai.bondings.callpilot.content

import ai.bondings.callpilot.protocol.CallRecordContentClient
import ai.bondings.callpilot.protocol.CallRecordDetail
import ai.bondings.callpilot.protocol.CallRecordItem
import ai.bondings.callpilot.protocol.CallSource
import ai.bondings.callpilot.protocol.CallSummaryState
import ai.bondings.callpilot.protocol.CallTimelineItem
import ai.bondings.callpilot.protocol.CallTimelineKind
import ai.bondings.callpilot.protocol.HostedCloudException
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext

enum class CallHistorySyncStatus { IDLE, LOADING, LIVE, STALE, OFFLINE }

enum class CallSummaryPresentation { HIDDEN, PENDING, READY, FAILED }

data class CallDetailState(
    val detail: CallRecordDetail? = null,
    val timeline: List<CallTimelineItem> = emptyList(),
    val nextTimelineCursor: String? = null,
    val timelineCollectionRevision: String? = null,
    val syncStatus: CallHistorySyncStatus = CallHistorySyncStatus.IDLE,
    val errorCode: String? = null,
    val isLoadingMore: Boolean = false,
) {
    val hasMoreTimeline: Boolean get() = nextTimelineCursor != null
    val visibleTimeline: List<CallTimelineItem> get() = timeline.filter { it.kind != CallTimelineKind.UNKNOWN }
    val summaryPresentation: CallSummaryPresentation
        get() = when (detail?.record?.summaryState) {
            CallSummaryState.PENDING -> CallSummaryPresentation.PENDING
            CallSummaryState.READY -> CallSummaryPresentation.READY
            CallSummaryState.FAILED -> CallSummaryPresentation.FAILED
            CallSummaryState.UNAVAILABLE, null -> CallSummaryPresentation.HIDDEN
        }
    val isNormalNoAIContent: Boolean
        get() = detail?.record?.source == CallSource.REMOTE_HANDSET &&
            detail.record.summaryState == CallSummaryState.UNAVAILABLE &&
            !detail.record.hasTranscript && visibleTimeline.isEmpty()
}

data class CallHistoryState(
    val records: List<CallRecordItem> = emptyList(),
    val syncStatus: CallHistorySyncStatus = CallHistorySyncStatus.IDLE,
    val errorCode: String? = null,
    val isRefreshing: Boolean = false,
    val isLoadingMore: Boolean = false,
    val collectionRevision: String? = null,
    val hasMore: Boolean = false,
    val details: Map<String, CallDetailState> = emptyMap(),
)

class CallHistoryModel(
    private val client: CallRecordContentClient,
    private val store: CallHistoryCacheStoring,
    private val deviceId: String,
    private val clockMs: () -> Long = System::currentTimeMillis,
    private val onUnauthorized: () -> Unit = {},
) {
    private val mutableState = MutableStateFlow(CallHistoryState())
    val state: StateFlow<CallHistoryState> = mutableState.asStateFlow()

    private var nextCursor: String? = null
    private var cacheLoaded = false
    private val generation = AtomicInteger(0)
    private val loadingDetails = mutableSetOf<String>()

    suspend fun loadCachedContent() = loadCacheIfNeeded()

    suspend fun refresh() {
        if (state.value.isRefreshing || state.value.isLoadingMore) return
        loadCacheIfNeeded()
        val requestGeneration = generation.get()
        mutableState.value = state.value.copy(
            isRefreshing = true,
            syncStatus = if (state.value.records.isEmpty()) CallHistorySyncStatus.LOADING else state.value.syncStatus,
        )
        try {
            val page = client.listCallRecords(25, null)
            if (requestGeneration != generation.get()) return
            val freshIds = page.items.mapTo(mutableSetOf(), CallRecordItem::callId)
            val records = (page.items + state.value.records.filterNot { it.callId in freshIds }).take(MAX_RECORDS)
            nextCursor = page.nextCursor
            mutableState.value = state.value.copy(
                records = records,
                syncStatus = CallHistorySyncStatus.LIVE,
                errorCode = null,
                collectionRevision = page.collectionRevision,
                hasMore = nextCursor != null,
            )
            saveCache()
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            if (requestGeneration == generation.get()) handleListError(error)
        } finally {
            if (requestGeneration == generation.get()) {
                mutableState.value = state.value.copy(isRefreshing = false)
            }
        }
    }

    suspend fun loadMore() {
        val cursor = nextCursor ?: return
        if (state.value.isRefreshing || state.value.isLoadingMore) return
        val requestGeneration = generation.get()
        mutableState.value = state.value.copy(isLoadingMore = true)
        try {
            val page = client.listCallRecords(25, cursor)
            if (requestGeneration != generation.get()) return
            val records = state.value.records.toMutableList()
            val indexes = records.mapIndexed { index, record -> record.callId to index }.toMap().toMutableMap()
            page.items.forEach { record ->
                val index = indexes[record.callId]
                if (index == null) {
                    indexes[record.callId] = records.size
                    records += record
                } else if (records[index].revision != record.revision) {
                    records[index] = record
                }
            }
            val bounded = records.take(MAX_RECORDS)
            nextCursor = page.nextCursor
            mutableState.value = state.value.copy(
                records = bounded,
                syncStatus = CallHistorySyncStatus.LIVE,
                errorCode = null,
                collectionRevision = page.collectionRevision,
                hasMore = nextCursor != null,
            )
            saveCache()
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            if (requestGeneration == generation.get()) handleListError(error)
        } finally {
            if (requestGeneration == generation.get()) {
                mutableState.value = state.value.copy(isLoadingMore = false)
            }
        }
    }

    suspend fun refreshDetail(callId: String) {
        if (!loadingDetails.add(callId)) return
        loadCacheIfNeeded()
        val requestGeneration = generation.get()
        updateDetail(callId) { current ->
            if (current.detail == null) current.copy(syncStatus = CallHistorySyncStatus.LOADING) else current
        }
        try {
            val detail = client.getCallRecord(callId)
            if (requestGeneration != generation.get()) return
            if (detail.record.callId != callId) {
                throw HostedCloudException(200, "INVALID_RESPONSE", "Call record identifier mismatch")
            }
            updateDetail(callId) {
                it.copy(detail = detail, errorCode = null)
            }
            replaceListRecord(detail.record)
            saveCache()

            val timeline = client.listCallTimeline(callId, 50, null)
            if (requestGeneration != generation.get()) return
            updateDetail(callId) {
                it.copy(
                    timeline = timeline.items.take(MAX_TIMELINE_ITEMS),
                    nextTimelineCursor = timeline.nextCursor,
                    timelineCollectionRevision = timeline.collectionRevision,
                    syncStatus = CallHistorySyncStatus.LIVE,
                    errorCode = null,
                )
            }
            saveCache()
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            if (requestGeneration == generation.get()) handleDetailError(error, callId)
        } finally {
            loadingDetails.remove(callId)
        }
    }

    suspend fun loadMoreTimeline(callId: String) {
        val current = state.value.details[callId] ?: return
        val cursor = current.nextTimelineCursor ?: return
        if (current.isLoadingMore) return
        val requestGeneration = generation.get()
        updateDetail(callId) { it.copy(isLoadingMore = true) }
        try {
            val page = client.listCallTimeline(callId, 50, cursor)
            if (requestGeneration != generation.get()) return
            val timeline = (state.value.details[callId]?.timeline ?: emptyList()).toMutableList()
            val indexes = timeline.mapIndexed { index, item -> item.timelineItemId to index }.toMap().toMutableMap()
            page.items.forEach { item ->
                val index = indexes[item.timelineItemId]
                if (index == null) {
                    indexes[item.timelineItemId] = timeline.size
                    timeline += item
                } else {
                    timeline[index] = item
                }
            }
            updateDetail(callId) {
                it.copy(
                    timeline = timeline.take(MAX_TIMELINE_ITEMS),
                    nextTimelineCursor = page.nextCursor,
                    timelineCollectionRevision = page.collectionRevision,
                    syncStatus = CallHistorySyncStatus.LIVE,
                    errorCode = null,
                )
            }
            saveCache()
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            if (requestGeneration == generation.get()) handleDetailError(error, callId)
        } finally {
            if (requestGeneration == generation.get()) {
                updateDetail(callId) { it.copy(isLoadingMore = false) }
            }
        }
    }

    suspend fun clearLocalData() {
        generation.incrementAndGet()
        nextCursor = null
        cacheLoaded = true
        mutableState.value = CallHistoryState()
        withContext(Dispatchers.IO) { runCatching(store::clear) }
    }

    private suspend fun loadCacheIfNeeded() {
        if (cacheLoaded) return
        cacheLoaded = true
        val loadGeneration = generation.get()
        val snapshot = withContext(Dispatchers.IO) { runCatching { store.load(deviceId) }.getOrNull() }
            ?: return
        if (loadGeneration != generation.get()) return
        val records = snapshot.records.take(MAX_RECORDS)
        val allowedDetailIds = records.take(MAX_DETAILS).mapTo(mutableSetOf(), CallRecordItem::callId)
        val details = snapshot.details.mapNotNull { (callId, cached) ->
            if (callId !in allowedDetailIds) return@mapNotNull null
            callId to CallDetailState(
                detail = cached.detail,
                timeline = cached.timeline.take(MAX_TIMELINE_ITEMS),
                nextTimelineCursor = cached.nextTimelineCursor,
                timelineCollectionRevision = cached.timelineCollectionRevision,
                syncStatus = CallHistorySyncStatus.STALE,
            )
        }.toMap()
        mutableState.value = state.value.copy(
            records = records,
            syncStatus = if (records.isEmpty()) CallHistorySyncStatus.IDLE else CallHistorySyncStatus.STALE,
            collectionRevision = snapshot.collectionRevision,
            details = details,
        )
        if (
            snapshot.records.size > MAX_RECORDS || snapshot.details.size > MAX_DETAILS ||
            snapshot.details.values.any { it.timeline.size > MAX_TIMELINE_ITEMS }
        ) {
            saveCache()
        }
    }

    private fun replaceListRecord(record: CallRecordItem) {
        val index = state.value.records.indexOfFirst { it.callId == record.callId }
        if (index < 0) return
        val records = state.value.records.toMutableList().also { it[index] = record }
        mutableState.value = state.value.copy(records = records)
    }

    private suspend fun handleListError(error: Exception) {
        val code = (error as? HostedCloudException)?.errorCode
        if (code == "UNAUTHORIZED") {
            clearLocalData()
            mutableState.value = state.value.copy(
                syncStatus = CallHistorySyncStatus.OFFLINE,
                errorCode = code,
            )
            onUnauthorized()
            return
        }
        mutableState.value = state.value.copy(
            syncStatus = if (state.value.records.isEmpty()) CallHistorySyncStatus.OFFLINE else CallHistorySyncStatus.STALE,
            errorCode = code,
        )
    }

    private suspend fun handleDetailError(error: Exception, callId: String) {
        val code = (error as? HostedCloudException)?.errorCode
        if (code == "UNAUTHORIZED") {
            handleListError(error)
            return
        }
        updateDetail(callId) {
            it.copy(
                syncStatus = if (it.detail == null) CallHistorySyncStatus.OFFLINE else CallHistorySyncStatus.STALE,
                errorCode = code,
            )
        }
    }

    private fun updateDetail(callId: String, transform: (CallDetailState) -> CallDetailState) {
        val details = state.value.details.toMutableMap()
        details[callId] = transform(details[callId] ?: CallDetailState())
        mutableState.value = state.value.copy(details = details)
    }

    private suspend fun saveCache() {
        val saveGeneration = generation.get()
        val snapshotState = state.value
        val allowedDetailIds = snapshotState.records.take(MAX_DETAILS).mapTo(mutableSetOf(), CallRecordItem::callId)
        val details = snapshotState.details.mapNotNull { (callId, state) ->
            val detail = state.detail ?: return@mapNotNull null
            if (callId !in allowedDetailIds) return@mapNotNull null
            callId to CachedCallDetail(
                detail = detail,
                timeline = state.timeline.take(MAX_TIMELINE_ITEMS),
                nextTimelineCursor = state.nextTimelineCursor,
                timelineCollectionRevision = state.timelineCollectionRevision,
            )
        }.toMap()
        val snapshot = CallHistoryCacheSnapshot(
            deviceId = deviceId,
            records = snapshotState.records.take(MAX_RECORDS),
            collectionRevision = snapshotState.collectionRevision,
            details = details,
            savedAt = clockMs(),
        )
        withContext(Dispatchers.IO) {
            runCatching {
                store.save(snapshot)
                if (saveGeneration != generation.get()) store.clear()
            }
        }
    }

    private companion object {
        const val MAX_RECORDS = 500
        const val MAX_DETAILS = 50
        const val MAX_TIMELINE_ITEMS = 500
    }
}
