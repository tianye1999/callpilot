package ai.bondings.callpilot.call

import ai.bondings.callpilot.media.RemoteSession
import ai.bondings.callpilot.media.SessionEvent
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.DeviceCredential
import ai.bondings.callpilot.protocol.Invite
import ai.bondings.callpilot.protocol.Signaling
import java.util.Base64
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CallManagerTest {

    private class FakeSession : RemoteSession {
        val commands = mutableListOf<String>()
        var connected = false
        var disconnected = false
        private val _events = MutableSharedFlow<SessionEvent>(extraBufferCapacity = 16)
        override val events: SharedFlow<SessionEvent> = _events

        override suspend fun connect(livekitUrl: String, token: String) {
            connected = true
        }

        override suspend fun sendCommand(json: String) {
            commands += json
        }

        override fun setSpeakerphone(enabled: Boolean) = Unit

        override fun disconnect() {
            disconnected = true
        }

        suspend fun emit(event: SessionEvent) = _events.emit(event)
    }

    private val pairing = StoredPairing(
        gatewayUrl = "https://gw.example.com",
        displayName = "Test",
        credential = DeviceCredential("dev", "secret"),
    )

    /** Edge 首选形态：结构化 livekit_url + token。 */
    private fun invite(sessionId: String = "s-1"): Invite = Invite(
        sessionId = sessionId,
        url = "https://d.example.com/#pair=",
        token = "tok",
        livekitUrl = "wss://lk.example.com",
        expiresAt = 9999.0,
    )

    /** 兼容形态：仅 url fragment（web 邀请），无结构化字段，走回退解析。 */
    private fun legacyInvite(sessionId: String = "s-1"): Invite {
        val payload =
            """{"v":1,"url":"wss://lk.example.com","token":"tok","sessionId":"$sessionId"}"""
        val fragment = Base64.getUrlEncoder().withoutPadding()
            .encodeToString(payload.toByteArray(Charsets.UTF_8))
        return Invite(sessionId = sessionId, url = "https://d.example.com/p.html#$fragment")
    }

    /** 用测试调度器驱动 manager 的 scope 与 IO，保证 advanceUntilIdle 完全确定。 */
    private class Harness(testScope: TestScope) {
        val dispatcher = StandardTestDispatcher(testScope.testScheduler)
        val scope = CoroutineScope(SupervisorJob() + dispatcher)
        val foreground = mutableListOf<Boolean>()

        fun manager(
            session: RemoteSession,
            inviteProvider: (StoredPairing) -> Invite,
        ): CallManager = CallManager(
            sessionFactory = { session },
            inviteProvider = inviteProvider,
            onForeground = { foreground += it },
            scope = scope,
            ioDispatcher = dispatcher,
        )

        fun close() = scope.cancel()
    }

    @Test
    fun `完整生命周期 拨号到本地挂断`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        val manager = h.manager(session) { invite() }

        manager.startCall(pairing, "10000")
        advanceUntilIdle()

        assertTrue(session.connected)
        assertEquals(CallState.WaitingMedia("10000"), manager.state.value)
        assertTrue(session.commands[0].contains("\"dial\""))
        assertTrue(session.commands[0].contains("10000"))
        assertEquals(listOf(true), h.foreground)

        session.emit(SessionEvent.Edge(Signaling.Event.RemoteCall("dialing")))
        advanceUntilIdle()
        assertEquals(CallState.Dialing("10000"), manager.state.value)

        session.emit(SessionEvent.Edge(Signaling.Event.RemoteCall("connected")))
        advanceUntilIdle()
        assertEquals(CallState.InCall("10000"), manager.state.value)

        manager.hangup()
        advanceUntilIdle()
        assertTrue(session.commands.last().contains("\"hangup\""))
        assertEquals(CallState.Ended("10000", "local_hangup"), manager.state.value)
        assertTrue(session.disconnected)
        assertEquals(listOf(true, false), h.foreground)

        manager.reset()
        assertEquals(CallState.Idle, manager.state.value)
        h.close()
    }

    @Test
    fun `Edge 结束事件驱动收尾`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        val manager = h.manager(session) { invite() }
        manager.startCall(pairing, "10000")
        advanceUntilIdle()

        session.emit(SessionEvent.Edge(Signaling.Event.RemoteCall("ended")))
        advanceUntilIdle()
        assertEquals(CallState.Ended("10000", "ended"), manager.state.value)
        assertTrue(session.disconnected)
        h.close()
    }

    @Test
    fun `房间断连时安全收尾`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        val manager = h.manager(session) { invite() }
        manager.startCall(pairing, "10000")
        advanceUntilIdle()

        session.emit(SessionEvent.Disconnected("network_lost"))
        advanceUntilIdle()
        assertEquals(CallState.Ended("10000", "network_lost"), manager.state.value)
        assertTrue(session.disconnected)
        h.close()
    }

    @Test
    fun `邀请获取失败进入 Failed`() = runTest {
        val h = Harness(this)
        val manager = h.manager(FakeSession()) { throw IllegalStateException("线路正在使用") }
        manager.startCall(pairing, "10000")
        advanceUntilIdle()
        val state = manager.state.value
        assertTrue(state is CallState.Failed && state.reason.contains("线路"))
        h.close()
    }

    @Test
    fun `邀请既无结构化字段又无合法 fragment 时 Failed`() = runTest {
        val h = Harness(this)
        val manager = h.manager(FakeSession()) {
            Invite(sessionId = "s", url = "https://d/p.html#not-base64!!!")
        }
        manager.startCall(pairing, "10000")
        advanceUntilIdle()
        assertTrue(manager.state.value is CallState.Failed)
        h.close()
    }

    @Test
    fun `仅 url fragment 的兼容邀请也能连上`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        val manager = h.manager(session) { legacyInvite() }
        manager.startCall(pairing, "10000")
        advanceUntilIdle()
        assertTrue(session.connected)
        assertEquals(CallState.WaitingMedia("10000"), manager.state.value)
        h.close()
    }

    @Test
    fun `通话中重复拨号被忽略`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        var invites = 0
        val manager = h.manager(session) { invites++; invite() }
        manager.startCall(pairing, "10000")
        advanceUntilIdle()
        manager.startCall(pairing, "10086")
        advanceUntilIdle()
        assertEquals(1, invites)
        assertEquals(CallState.WaitingMedia("10000"), manager.state.value)
        h.close()
    }
}
