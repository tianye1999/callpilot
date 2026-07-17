package ai.bondings.callpilot.content

import ai.bondings.callpilot.protocol.CallRecordContentClient
import ai.bondings.callpilot.protocol.CallRecordDetail
import ai.bondings.callpilot.protocol.CallRecordsPage
import ai.bondings.callpilot.protocol.CallSummaryState
import ai.bondings.callpilot.protocol.CallTimelinePage
import ai.bondings.callpilot.protocol.ContentTestFixtures
import ai.bondings.callpilot.protocol.HostedCloudException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CallHistoryModelTest {
    @Test
    fun `late summary replaces the list item in place and detail exposes ready summary`() = runTest {
        val pending = pendingDetail()
        val ready = readyDetail()
        val firstPage = recordsPage().copy(items = listOf(pending.record))
        val secondPage = recordsPage().copy(items = listOf(ready.record))
        val client = FakeCallContentClient(
            recordPages = mutableListOf(Result.success(firstPage), Result.success(secondPage)),
            details = mutableListOf(ready),
            timelines = mutableListOf(timelinePage()),
        )
        val model = CallHistoryModel(client, InMemoryCallHistoryCache(), DEVICE_ID)

        model.refresh()
        assertEquals(CallSummaryState.PENDING, model.state.value.records.single().summaryState)

        model.refresh()
        assertEquals(1, model.state.value.records.size)
        assertEquals(CallSummaryState.READY, model.state.value.records.single().summaryState)

        model.refreshDetail(ready.record.callId)
        val detail = model.state.value.details.getValue(ready.record.callId)
        assertEquals(CallSummaryPresentation.READY, detail.summaryPresentation)
        assertEquals(5, detail.visibleTimeline.size)
    }

    @Test
    fun `remote handset without AI content is a normal detail state`() = runTest {
        val remote = remoteDetail()
        val model = CallHistoryModel(
            FakeCallContentClient(
                details = mutableListOf(remote),
                timelines = mutableListOf(emptyTimelinePage()),
            ),
            InMemoryCallHistoryCache(),
            DEVICE_ID,
        )

        model.refreshDetail(remote.record.callId)

        val state = model.state.value.details.getValue(remote.record.callId)
        assertTrue(state.isNormalNoAIContent)
        assertEquals(CallSummaryPresentation.HIDDEN, state.summaryPresentation)
        assertTrue(state.visibleTimeline.isEmpty())
    }

    @Test
    fun `summary presentation covers unavailable pending ready and failed without guessing`() {
        val pending = pendingDetail()
        val ready = readyDetail()
        val remote = remoteDetail()
        val failed = ready.copy(
            record = ready.record.copy(
                revision = "revision_call_pending_0003",
                summaryState = CallSummaryState.FAILED,
                summaryPreview = null,
            ),
            summary = ready.summary?.copy(ok = false, text = null, errorCode = "SUMMARY_FAILED"),
        )

        assertEquals(CallSummaryPresentation.HIDDEN, CallDetailState(detail = remote).summaryPresentation)
        assertEquals(CallSummaryPresentation.PENDING, CallDetailState(detail = pending).summaryPresentation)
        assertEquals(CallSummaryPresentation.READY, CallDetailState(detail = ready).summaryPresentation)
        assertEquals(CallSummaryPresentation.FAILED, CallDetailState(detail = failed).summaryPresentation)
    }

    @Test
    fun `timeline pagination appends oldest first and hides future event types`() = runTest {
        val detail = readyDetail()
        val allItems = timelinePage().items
        val future = allItems.first().copy(
            timelineItemId = "item_fixture_future_0001",
            occurredAt = allItems.last().occurredAt + 1,
            type = "FUTURE_EVENT",
        )
        val first = timelinePage().copy(
            items = allItems.take(2),
            nextCursor = "cursor_timeline_fixture_0001",
            hasMore = true,
        )
        val second = timelinePage().copy(items = allItems.drop(2) + future)
        val model = CallHistoryModel(
            FakeCallContentClient(
                details = mutableListOf(detail),
                timelines = mutableListOf(first, second),
            ),
            InMemoryCallHistoryCache(),
            DEVICE_ID,
        )

        model.refreshDetail(detail.record.callId)
        model.loadMoreTimeline(detail.record.callId)

        val state = model.state.value.details.getValue(detail.record.callId)
        assertEquals(allItems + future, state.timeline)
        assertEquals(allItems, state.visibleTimeline)
        assertFalse(state.hasMoreTimeline)
    }

    @Test
    fun `pagination keeps item 101 and caps cache at edge retention 500`() = runTest {
        val items = (0..500).map(::bulkRecord)
        val pages = items.chunked(100).mapIndexed { index, records ->
            recordsPage().copy(
                items = records,
                nextCursor = if (index < 5) "cursor_fixture_calls_${index + 1}" else null,
                hasMore = index < 5,
            )
        }
        val store = InMemoryCallHistoryCache()
        val model = CallHistoryModel(
            FakeCallContentClient(recordPages = pages.mapTo(mutableListOf()) { Result.success(it) }),
            store,
            DEVICE_ID,
        )

        model.refresh()
        repeat(5) { model.loadMore() }

        assertEquals(500, model.state.value.records.size)
        assertEquals(items[100], model.state.value.records[100])
        assertEquals(items[499], model.state.value.records.last())
        assertFalse(model.state.value.hasMore)
        assertEquals(500, store.snapshot?.records?.size)
    }

    @Test
    fun `clear fences late detail and unauthorized clears all protected content`() = runTest {
        val suspended = SuspendedCallContentClient()
        val store = InMemoryCallHistoryCache()
        val model = CallHistoryModel(suspended, store, DEVICE_ID)
        val request = async { model.refreshDetail(readyDetail().record.callId) }
        suspended.started.await()

        model.clearLocalData()
        suspended.detail.complete(readyDetail())
        request.await()

        assertTrue(model.state.value.details.isEmpty())
        assertNull(store.snapshot)

        store.snapshot = snapshot(recordsPage())
        var unauthorized = false
        val revoked = CallHistoryModel(
            FakeCallContentClient(
                recordPages = mutableListOf(
                    Result.failure(HostedCloudException(401, "UNAUTHORIZED", "must not display")),
                ),
            ),
            store,
            DEVICE_ID,
            onUnauthorized = { unauthorized = true },
        )
        revoked.refresh()

        assertTrue(unauthorized)
        assertTrue(revoked.state.value.records.isEmpty())
        assertNull(store.snapshot)
        assertEquals(CallHistorySyncStatus.OFFLINE, revoked.state.value.syncStatus)
    }

    private fun recordsPage() = CallRecordsPage.decode(json, ContentTestFixtures.text("call-records-page.json"))
    private fun pendingDetail() = CallRecordDetail.decode(json, ContentTestFixtures.text("call-record-detail-pending.json"))
    private fun readyDetail() = CallRecordDetail.decode(json, ContentTestFixtures.text("call-record-detail-ready.json"))
    private fun remoteDetail() = CallRecordDetail.decode(json, ContentTestFixtures.text("call-record-detail-no-transcript.json"))
    private fun timelinePage() = CallTimelinePage.decode(json, ContentTestFixtures.text("call-timeline-page.json"))
    private fun emptyTimelinePage() = CallTimelinePage.decode(json, ContentTestFixtures.text("call-timeline-empty.json"))

    private fun bulkRecord(index: Int) = recordsPage().items.first().copy(
        callId = "call_fixture_bulk_${index.toString().padStart(4, '0')}",
        revision = "revision_call_bulk_${index.toString().padStart(4, '0')}",
        startedAt = 2_000_000L - index,
        endedAt = 2_000_100L - index,
    )

    private fun snapshot(page: CallRecordsPage) = CallHistoryCacheSnapshot(
        deviceId = DEVICE_ID,
        records = page.items,
        collectionRevision = page.collectionRevision,
        details = emptyMap(),
        savedAt = 1,
    )

    private companion object {
        val json = Json { ignoreUnknownKeys = true }
        const val DEVICE_ID = "device_abcdefghijkl"
    }
}

private class FakeCallContentClient(
    private val recordPages: MutableList<Result<CallRecordsPage>> = mutableListOf(),
    private val details: MutableList<CallRecordDetail> = mutableListOf(),
    private val timelines: MutableList<CallTimelinePage> = mutableListOf(),
) : CallRecordContentClient {
    override suspend fun listCallRecords(limit: Int, cursor: String?): CallRecordsPage =
        recordPages.removeFirst().getOrThrow()

    override suspend fun getCallRecord(callId: String): CallRecordDetail = details.removeFirst()

    override suspend fun listCallTimeline(callId: String, limit: Int, cursor: String?): CallTimelinePage =
        timelines.removeFirst()
}

private class SuspendedCallContentClient : CallRecordContentClient {
    val started = CompletableDeferred<Unit>()
    val detail = CompletableDeferred<CallRecordDetail>()

    override suspend fun listCallRecords(limit: Int, cursor: String?): CallRecordsPage = error("unused")

    override suspend fun getCallRecord(callId: String): CallRecordDetail {
        started.complete(Unit)
        return detail.await()
    }

    override suspend fun listCallTimeline(callId: String, limit: Int, cursor: String?): CallTimelinePage = error("fenced")
}

private class InMemoryCallHistoryCache(
    var snapshot: CallHistoryCacheSnapshot? = null,
) : CallHistoryCacheStoring {
    override fun load(deviceId: String): CallHistoryCacheSnapshot? = snapshot?.takeIf { it.deviceId == deviceId }

    override fun save(snapshot: CallHistoryCacheSnapshot) {
        this.snapshot = snapshot
    }

    override fun clear() {
        snapshot = null
    }
}
