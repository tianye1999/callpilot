package ai.bondings.callpilot.content

import ai.bondings.callpilot.protocol.ContentTestFixtures
import ai.bondings.callpilot.protocol.HostedCloudException
import ai.bondings.callpilot.protocol.MessageContentClient
import ai.bondings.callpilot.protocol.MessagePage
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class MessageInboxModelTest {
    @Test
    fun `notification detail loads a message without first visiting inbox`() = runTest {
        val target = bulkMessage(30)
        val first = fixturePage().copy(items = listOf(bulkMessage(0)), hasMore = true, nextCursor = "cursor_next")
        val last = first.copy(items = listOf(target), hasMore = false, nextCursor = null)
        val model = MessageInboxModel(
            FakeMessageClient(mutableListOf(Result.success(first), Result.success(last))),
            InMemoryMessageCache(), DEVICE_ID,
        )
        model.loadMessage(target.messageId)
        assertEquals(target, model.state.value.messages.last())
    }

    @Test
    fun `notification detail refreshes cached revision and stops on network error`() = runTest {
        val old = bulkMessage(0)
        val updated = old.copy(revision = "revision_updated_000000000001", text = "Test message continued")
        val page = fixturePage().copy(items = listOf(updated), hasMore = false, nextCursor = null)
        val model = MessageInboxModel(
            FakeMessageClient(mutableListOf(Result.success(page), Result.failure(java.io.IOException()))),
            InMemoryMessageCache(MessageCacheSnapshot(DEVICE_ID, listOf(old), null, page.collectionRevision, 1_000)),
            DEVICE_ID,
        )
        model.loadMessage(old.messageId)
        assertEquals(updated, model.state.value.messages.single())
        model.loadMessage(bulkMessage(1).messageId)
        assertEquals(MessageSyncStatus.STALE, model.state.value.syncStatus)
    }

    @Test
    fun `notification detail stops when pagination cursor does not advance`() = runTest {
        val page = fixturePage().copy(items = emptyList(), hasMore = true, nextCursor = "cursor_same")
        val model = MessageInboxModel(
            FakeMessageClient(mutableListOf(Result.success(page), Result.success(page))),
            InMemoryMessageCache(), DEVICE_ID,
        )
        model.loadMessage(bulkMessage(0).messageId)
        assertTrue(model.state.value.messages.isEmpty())
    }

    @Test
    fun `refresh is live and watermark advances only after visible display`() = runTest {
        val page = fixturePage()
        val store = InMemoryMessageCache()
        val model = MessageInboxModel(
            client = FakeMessageClient(mutableListOf(Result.success(page))),
            store = store,
            deviceId = DEVICE_ID,
            clockMs = { 2_000 },
        )

        model.refresh()
        assertEquals(MessageSyncStatus.LIVE, model.state.value.syncStatus)
        assertEquals(3, model.state.value.unreadCount)
        assertNull(store.snapshot?.watermark)

        model.setVisible(true)
        model.markLatestDisplayed()
        assertEquals(0, model.state.value.unreadCount)
        assertEquals(page.items.first().messageId, store.snapshot?.watermark?.messageId)
    }

    @Test
    fun `failed refresh keeps cache stale and stable error copy`() = runTest {
        val page = fixturePage()
        val watermark = MessageWatermark(page.items.last().messageId, page.items.last().occurredAt)
        val store = InMemoryMessageCache(
            MessageCacheSnapshot(DEVICE_ID, page.items, watermark, page.collectionRevision, 1_000),
        )
        val model = MessageInboxModel(
            client = FakeMessageClient(
                mutableListOf(
                    Result.failure(HostedCloudException(413, "PAYLOAD_TOO_LARGE", "must not display")),
                ),
            ),
            store = store,
            deviceId = DEVICE_ID,
        )
        model.setVisible(true)

        model.refresh()
        model.markLatestDisplayed()

        assertEquals(MessageSyncStatus.STALE, model.state.value.syncStatus)
        assertEquals("PAYLOAD_TOO_LARGE", model.state.value.errorCode)
        assertEquals(2, model.state.value.unreadCount)
        assertEquals(watermark, store.snapshot?.watermark)
    }

    @Test
    fun `pagination keeps item 101 and is capped at edge retention 500`() = runTest {
        val items = (0..500).map(::bulkMessage)
        val pages = items.chunked(100).mapIndexed { index, pageItems ->
            fixturePage().copy(
                items = pageItems,
                nextCursor = if (index < 5) "cursor_fixture_bulk_${(index + 1).toString().padStart(4, '0')}" else null,
                hasMore = index < 5,
            )
        }
        val store = InMemoryMessageCache()
        val model = MessageInboxModel(
            FakeMessageClient(pages.mapTo(mutableListOf()) { Result.success(it) }),
            store,
            DEVICE_ID,
        )

        model.refresh()
        repeat(5) { model.loadMore() }

        assertEquals(500, model.state.value.messages.size)
        assertEquals(items[100], model.state.value.messages[100])
        assertEquals(items[499], model.state.value.messages.last())
        assertFalse(model.state.value.hasMore)
        assertEquals(500, store.snapshot?.messages?.size)
    }

    @Test
    fun `clear fences a late refresh and unauthorized wipes local state`() = runTest {
        val suspended = SuspendedMessageClient()
        val store = InMemoryMessageCache()
        val model = MessageInboxModel(suspended, store, DEVICE_ID)
        val refresh = async { model.refresh() }
        suspended.started.await()

        model.clearLocalData()
        suspended.result.complete(fixturePage())
        refresh.await()

        assertTrue(model.state.value.messages.isEmpty())
        assertNull(store.snapshot)

        var unauthorized = false
        val revoked = MessageInboxModel(
            FakeMessageClient(
                mutableListOf(Result.failure(HostedCloudException(401, "UNAUTHORIZED", "revoked"))),
            ),
            store,
            DEVICE_ID,
            onUnauthorized = { unauthorized = true },
        )
        revoked.refresh()
        assertTrue(unauthorized)
        assertEquals(MessageSyncStatus.OFFLINE, revoked.state.value.syncStatus)
    }

    private fun fixturePage(): MessagePage = MessagePage.decode(
        Json { ignoreUnknownKeys = true },
        ContentTestFixtures.text("messages-page.json"),
    )

    private fun bulkMessage(index: Int) = fixturePage().items.first().copy(
        messageId = "msg_fixture_bulk_${index.toString().padStart(4, '0')}",
        revision = "revision_fixture_bulk_${index.toString().padStart(4, '0')}",
        occurredAt = 2_000_000L - index,
        recordedAt = 2_000_000L - index,
    )

    private companion object {
        const val DEVICE_ID = "device_abcdefghijkl"
    }
}

private class FakeMessageClient(
    private val results: MutableList<Result<MessagePage>>,
) : MessageContentClient {
    override suspend fun listMessages(limit: Int, cursor: String?): MessagePage =
        results.removeFirst().getOrThrow()
}

private class SuspendedMessageClient : MessageContentClient {
    val started = CompletableDeferred<Unit>()
    val result = CompletableDeferred<MessagePage>()

    override suspend fun listMessages(limit: Int, cursor: String?): MessagePage {
        started.complete(Unit)
        return result.await()
    }
}

private class InMemoryMessageCache(
    var snapshot: MessageCacheSnapshot? = null,
) : MessageCacheStoring {
    override fun load(deviceId: String): MessageCacheSnapshot? =
        snapshot?.takeIf { it.deviceId == deviceId }

    override fun save(snapshot: MessageCacheSnapshot) {
        this.snapshot = snapshot
    }

    override fun clear() {
        snapshot = null
    }
}
