package ai.bondings.callpilot.content

import ai.bondings.callpilot.protocol.HostedCloudException
import ai.bondings.callpilot.protocol.MessageContentClient
import ai.bondings.callpilot.protocol.SMSMessage
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext

enum class MessageSyncStatus { IDLE, LOADING, LIVE, STALE, OFFLINE }

object MessageCopy {
    const val PAYLOAD_TOO_LARGE = "这条短信过长，当前版本暂时无法显示"
    const val UNAVAILABLE = "暂时无法载入短信"
    const val EDGE_OFFLINE = "电脑端离线，正在显示本机缓存"
    const val FEATURE_DISABLED = "短信同步尚未启用"
    const val UNAUTHORIZED = "设备授权已失效，请重新配对"
}

data class MessageInboxState(
    val messages: List<SMSMessage> = emptyList(),
    val syncStatus: MessageSyncStatus = MessageSyncStatus.IDLE,
    val unreadCount: Int = 0,
    val errorCode: String? = null,
    val errorMessage: String? = null,
    val isRefreshing: Boolean = false,
    val isLoadingMore: Boolean = false,
    val collectionRevision: String? = null,
    val hasMore: Boolean = false,
)

class MessageInboxModel(
    private val client: MessageContentClient,
    private val store: MessageCacheStoring,
    private val deviceId: String,
    private val clockMs: () -> Long = System::currentTimeMillis,
    private val onUnauthorized: () -> Unit = {},
) {
    private val mutableState = MutableStateFlow(MessageInboxState())
    val state: StateFlow<MessageInboxState> = mutableState.asStateFlow()

    private var watermark: MessageWatermark? = null
    private var nextCursor: String? = null
    private var cacheLoaded = false
    private var visible = false
    private val generation = AtomicInteger(0)

    suspend fun refresh() {
        if (state.value.isRefreshing || state.value.isLoadingMore) return
        loadCacheIfNeeded()
        val requestGeneration = generation.get()
        mutableState.value = state.value.copy(
            isRefreshing = true,
            syncStatus = if (state.value.messages.isEmpty()) MessageSyncStatus.LOADING else state.value.syncStatus,
        )
        try {
            val page = client.listMessages(25, null)
            if (requestGeneration != generation.get()) return
            val merged = mergeFirstPage(page.items, state.value.messages)
            nextCursor = page.nextCursor
            mutableState.value = state.value.copy(
                messages = merged,
                syncStatus = MessageSyncStatus.LIVE,
                unreadCount = unreadCount(merged),
                errorCode = null,
                errorMessage = null,
                collectionRevision = page.collectionRevision,
                hasMore = nextCursor != null,
            )
            saveCache()
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            if (requestGeneration == generation.get()) handle(error)
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
            val page = client.listMessages(25, cursor)
            if (requestGeneration != generation.get()) return
            val merged = state.value.messages.toMutableList()
            val indexes = merged.mapIndexed { index, message -> message.messageId to index }.toMap().toMutableMap()
            page.items.forEach { message ->
                val index = indexes[message.messageId]
                if (index == null) {
                    indexes[message.messageId] = merged.size
                    merged += message
                } else if (merged[index].revision != message.revision) {
                    merged[index] = message
                }
            }
            val bounded = merged.take(MAX_MESSAGES)
            nextCursor = page.nextCursor
            mutableState.value = state.value.copy(
                messages = bounded,
                syncStatus = MessageSyncStatus.LIVE,
                unreadCount = unreadCount(bounded),
                errorCode = null,
                errorMessage = null,
                collectionRevision = page.collectionRevision,
                hasMore = nextCursor != null,
            )
            saveCache()
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            if (requestGeneration == generation.get()) handle(error)
        } finally {
            if (requestGeneration == generation.get()) {
                mutableState.value = state.value.copy(isLoadingMore = false)
            }
        }
    }

    suspend fun loadCachedContent() = loadCacheIfNeeded()

    fun setVisible(value: Boolean) {
        visible = value
    }

    suspend fun markLatestDisplayed() {
        val snapshot = state.value
        val latest = snapshot.messages.firstOrNull() ?: return
        if (!visible || snapshot.syncStatus != MessageSyncStatus.LIVE) return
        watermark = MessageWatermark(latest.messageId, latest.occurredAt)
        mutableState.value = snapshot.copy(unreadCount = 0)
        saveCache()
    }

    suspend fun clearLocalData() {
        generation.incrementAndGet()
        watermark = null
        nextCursor = null
        cacheLoaded = true
        mutableState.value = MessageInboxState()
        withContext(Dispatchers.IO) { runCatching(store::clear) }
    }

    private suspend fun loadCacheIfNeeded() {
        if (cacheLoaded) return
        cacheLoaded = true
        val loadGeneration = generation.get()
        val snapshot = withContext(Dispatchers.IO) { runCatching { store.load(deviceId) }.getOrNull() }
            ?: return
        if (loadGeneration != generation.get()) return
        val messages = snapshot.messages.take(MAX_MESSAGES)
        watermark = snapshot.watermark
        mutableState.value = state.value.copy(
            messages = messages,
            syncStatus = if (messages.isEmpty()) MessageSyncStatus.IDLE else MessageSyncStatus.STALE,
            unreadCount = unreadCount(messages),
            collectionRevision = snapshot.collectionRevision,
        )
        if (snapshot.messages.size > MAX_MESSAGES) saveCache()
    }

    private fun mergeFirstPage(fresh: List<SMSMessage>, cached: List<SMSMessage>): List<SMSMessage> {
        val freshIds = fresh.mapTo(mutableSetOf(), SMSMessage::messageId)
        return (fresh + cached.filterNot { it.messageId in freshIds }).take(MAX_MESSAGES)
    }

    private fun unreadCount(messages: List<SMSMessage>): Int {
        val marker = watermark ?: return messages.size
        val index = messages.indexOfFirst { it.messageId == marker.messageId }
        if (index >= 0) return index
        return messages.takeWhile {
            it.occurredAt > marker.occurredAt ||
                (it.occurredAt == marker.occurredAt && it.messageId > marker.messageId)
        }.size
    }

    private suspend fun handle(error: Exception) {
        val code = (error as? HostedCloudException)?.errorCode
        if (code == "UNAUTHORIZED") {
            clearLocalData()
            mutableState.value = state.value.copy(
                syncStatus = MessageSyncStatus.OFFLINE,
                errorCode = code,
                errorMessage = MessageCopy.UNAUTHORIZED,
            )
            onUnauthorized()
            return
        }
        val message = when (code) {
            "PAYLOAD_TOO_LARGE" -> MessageCopy.PAYLOAD_TOO_LARGE
            "EDGE_OFFLINE", "TIMEOUT" -> MessageCopy.EDGE_OFFLINE
            "FEATURE_DISABLED", "FORBIDDEN" -> MessageCopy.FEATURE_DISABLED
            else -> MessageCopy.UNAVAILABLE
        }
        mutableState.value = state.value.copy(
            syncStatus = if (state.value.messages.isEmpty()) MessageSyncStatus.OFFLINE else MessageSyncStatus.STALE,
            errorCode = code,
            errorMessage = message,
        )
    }

    private suspend fun saveCache() {
        val saveGeneration = generation.get()
        val snapshot = MessageCacheSnapshot(
            deviceId = deviceId,
            messages = state.value.messages.take(MAX_MESSAGES),
            watermark = watermark,
            collectionRevision = state.value.collectionRevision,
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
        const val MAX_MESSAGES = 500
    }
}
