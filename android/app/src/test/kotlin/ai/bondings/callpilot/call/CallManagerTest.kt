package ai.bondings.callpilot.call

import ai.bondings.callpilot.media.RemoteSession
import ai.bondings.callpilot.media.SessionEvent
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.DeviceCredential
import ai.bondings.callpilot.protocol.HostedCallSession
import ai.bondings.callpilot.protocol.HostedCloudException
import ai.bondings.callpilot.protocol.Invite
import ai.bondings.callpilot.protocol.PairingProtocol
import ai.bondings.callpilot.protocol.Signaling
import java.util.Base64
import kotlinx.coroutines.CompletableDeferred
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
import org.junit.Assert.assertFalse
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
            if (json.contains("\"dial\"")) {
                dialStarted.complete(Unit)
                dialRelease?.await()
            }
            commands += json
        }

        override fun setSpeakerphone(enabled: Boolean) = Unit

        override fun disconnect() {
            disconnected = true
        }

        val dialStarted = CompletableDeferred<Unit>()
        var dialRelease: CompletableDeferred<Unit>? = null

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
            hostedSessionProvider: suspend (StoredPairing, String) -> HostedCallSession = { _, _ ->
                error("hosted provider should not be called")
            },
            idempotencyKeyProvider: () -> String = { "android-test-idempotency" },
            inviteProvider: suspend (StoredPairing) -> Invite,
        ): CallManager = CallManager(
            sessionFactory = { session },
            inviteProvider = inviteProvider,
            hostedSessionProvider = hostedSessionProvider,
            idempotencyKeyProvider = idempotencyKeyProvider,
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
        // dial 必须等 Edge 确认 media_ready 才发出（Edge 会拒绝抢跑的 dial）
        assertTrue(session.commands.isEmpty())
        assertEquals(listOf(true), h.foreground)

        // Edge 实际经 data channel 发的是 type=status 事件（#37 契约）
        session.emit(SessionEvent.Edge(Signaling.Event.Status("media_ready")))
        advanceUntilIdle()
        assertEquals(CallState.WaitingMedia("10000"), manager.state.value)
        assertTrue(session.commands[0].contains("\"dial\""))
        assertTrue(session.commands[0].contains("10000"))

        // media_ready 重复到达不会重发 dial
        session.emit(SessionEvent.Edge(Signaling.Event.Status("media_ready")))
        advanceUntilIdle()
        assertEquals(1, session.commands.size)

        session.emit(SessionEvent.Edge(Signaling.Event.Status("dialing")))
        advanceUntilIdle()
        assertEquals(CallState.Dialing("10000"), manager.state.value)

        session.emit(SessionEvent.Edge(Signaling.Event.Status("connected")))
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

        session.emit(SessionEvent.Edge(Signaling.Event.Status("ended")))
        advanceUntilIdle()
        assertEquals(CallState.Ended("10000", "ended"), manager.state.value)
        assertTrue(session.disconnected)
        h.close()
    }

    @Test
    fun `Edge failed 事件进入 Failed`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        val manager = h.manager(session) { invite() }
        manager.startCall(pairing, "10000")
        advanceUntilIdle()

        session.emit(
            SessionEvent.Edge(Signaling.Event.Status("failed", reason = "line_unavailable"))
        )
        advanceUntilIdle()
        assertEquals(CallState.Failed("10000", "line_unavailable"), manager.state.value)
        assertTrue(session.disconnected)
        h.close()
    }

    @Test
    fun `remote_call 事件兼容推进状态`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        val manager = h.manager(session) { invite() }
        manager.startCall(pairing, "10000")
        advanceUntilIdle()

        session.emit(SessionEvent.Edge(Signaling.Event.RemoteCall("connected")))
        advanceUntilIdle()
        assertEquals(CallState.InCall("10000"), manager.state.value)
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

    @Test
    fun `hosted 配对选择 v1 provider 而不是 Tunnel provider`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        var tunnelCalls = 0
        var hostedCalls = 0
        val manager = h.manager(
            session = session,
            inviteProvider = { tunnelCalls++; invite() },
            hostedSessionProvider = { _, key ->
                hostedCalls++
                assertEquals("android-test-idempotency", key)
                HostedCallSession(
                    sessionId = "call_abcdefghijkl",
                    livekitUrl = "wss://lk.example.com",
                    token = "tok",
                    expiresAt = 9999,
                )
            },
        )
        val hostedPairing = pairing.copy(
            protocol = PairingProtocol.HOSTED,
            edgeId = "edge_abcdefghijkl",
        )

        manager.startCall(hostedPairing, "10000")
        advanceUntilIdle()

        assertEquals(0, tunnelCalls)
        assertEquals(1, hostedCalls)
        assertTrue(session.connected)
        assertEquals(CallState.WaitingMedia("10000"), manager.state.value)
        h.close()
    }

    @Test
    fun `Tunnel 配对继续选择 v0 provider`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        var tunnelCalls = 0
        var hostedCalls = 0
        val manager = h.manager(
            session = session,
            inviteProvider = {
                tunnelCalls++
                invite()
            },
            hostedSessionProvider = { _, _ ->
                hostedCalls++
                error("hosted provider should not be called")
            },
        )

        manager.startCall(pairing, "10000")
        advanceUntilIdle()

        assertEquals(1, tunnelCalls)
        assertEquals(0, hostedCalls)
        assertTrue(session.connected)
        assertEquals(CallState.WaitingMedia("10000"), manager.state.value)
        h.close()
    }

    @Test
    fun `hosted 会话轮询中挂断不会在轮询完成后复活通话`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        val sessionResult = CompletableDeferred<HostedCallSession>()
        val manager = h.manager(
            session = session,
            inviteProvider = { error("Tunnel provider should not be called") },
            hostedSessionProvider = { _, _ -> sessionResult.await() },
        )
        val hostedPairing = pairing.copy(
            protocol = PairingProtocol.HOSTED,
            edgeId = "edge_abcdefghijkl",
        )

        manager.startCall(hostedPairing, "10000")
        advanceUntilIdle()
        assertEquals(CallState.Preparing("10000"), manager.state.value)

        manager.hangup()
        advanceUntilIdle()
        sessionResult.complete(
            HostedCallSession(
                sessionId = "call_abcdefghijkl",
                livekitUrl = "wss://lk.example.com",
                token = "tok",
                expiresAt = 9999,
            )
        )
        advanceUntilIdle()

        assertEquals(CallState.Ended("10000", "local_hangup"), manager.state.value)
        assertFalse(session.connected)
        h.close()
    }

    @Test
    fun `挂断与 media_ready 交错时绝不发送 dial`() = runTest {
        val h = Harness(this)
        val session = FakeSession().also {
            it.dialRelease = CompletableDeferred()
        }
        val manager = h.manager(session) { invite() }

        manager.startCall(pairing, "10000")
        advanceUntilIdle()
        session.emit(SessionEvent.Edge(Signaling.Event.Status("media_ready")))
        advanceUntilIdle()
        assertTrue(session.dialStarted.isCompleted)

        manager.hangup()
        session.dialRelease?.complete(Unit)
        advanceUntilIdle()

        assertFalse(session.commands.any { it.contains("\"dial\"") })
        assertEquals(CallState.Ended("10000", "local_hangup"), manager.state.value)
        h.close()
    }

    @Test
    fun `hosted 业务错误透传稳定 code`() = runTest {
        val h = Harness(this)
        val manager = h.manager(
            session = FakeSession(),
            inviteProvider = { error("Tunnel provider should not be called") },
            hostedSessionProvider = { _, _ ->
                throw HostedCloudException(409, "LINE_BUSY", "Line is busy")
            },
        )
        val hostedPairing = pairing.copy(
            protocol = PairingProtocol.HOSTED,
            edgeId = "edge_abcdefghijkl",
        )

        manager.startCall(hostedPairing, "10000")
        advanceUntilIdle()

        assertEquals(
            CallState.Failed("10000", "Line is busy", code = "LINE_BUSY"),
            manager.state.value,
        )
        h.close()
    }
}
