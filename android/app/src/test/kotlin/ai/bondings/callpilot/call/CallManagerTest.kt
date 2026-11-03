package ai.bondings.callpilot.call

import ai.bondings.callpilot.media.DirectCapableSession
import ai.bondings.callpilot.media.RemoteSession
import ai.bondings.callpilot.media.SessionEvent
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.DeviceCredential
import ai.bondings.callpilot.protocol.HostedCallSession
import ai.bondings.callpilot.protocol.HostedCloudException
import ai.bondings.callpilot.protocol.IceServer
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
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CallManagerTest {

    private open class FakeSession : RemoteSession {
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

    private open class FakeDirectSession : FakeSession(), DirectCapableSession {
        val prepared = mutableListOf<List<IceServer>>()
        var answer: String? = null
        var closedDirect = 0
        var failPrepare = false
        var failConnect = false
        /** 不为 null 时 prepareDirect 挂起直到它完成（模拟 TURN 收集卡住）。 */
        var prepareGate: CompletableDeferred<Unit>? = null

        override suspend fun prepareDirect(iceServers: List<IceServer>): String {
            prepared += iceServers
            prepareGate?.await()
            if (failPrepare) error("no relay candidate")
            return "v=0\r\nfake-offer typ relay"
        }

        override suspend fun connectDirect(sdpAnswer: String) {
            if (failConnect) error("ice failed")
            answer = sdpAnswer
        }

        override fun closeDirect() {
            closedDirect += 1
        }
    }

    private class FakeGateway(
        private val claimResult: HostedCallSession,
        private val sdpResult: HostedCallSession? = null,
    ) : InboundMediaGateway {
        val calls = mutableListOf<String>()
        var claimGate: CompletableDeferred<Unit>? = null
        var fallbackGate: CompletableDeferred<Unit>? = null
        override suspend fun claim(offerId: String, direct: Boolean): HostedCallSession {
            calls += "claim:$direct"
            claimGate?.await()
            return claimResult
        }

        override suspend fun iceServers(offerId: String): List<IceServer> {
            calls += "ice"
            return listOf(IceServer(listOf("turn:turn.example:3478"), "u", "c"))
        }

        override suspend fun completeSdp(offerId: String, claimId: String, sdpOffer: String): HostedCallSession {
            calls += "sdp"
            return sdpResult ?: error("no sdp result")
        }

        override suspend fun fallbackLiveKit(offerId: String, claimId: String): HostedCallSession {
            calls += "fallback"
            fallbackGate?.await()
            return claimResult.copy(token = "fallback-token", directAttempted = true, sdpPending = false)
        }

        override suspend fun end(offerId: String, claimId: String) {
            calls += "end"
        }
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
            takeoverClaimProvider: suspend (StoredPairing, String) -> HostedCallSession = { _, _ ->
                error("takeover provider should not be called")
            },
            takeoverMediaTimeoutMs: Long = 20_000L,
            takeoverFailureVisibleMs: Long = 2_000L,
            directSession: DirectCapableSession? = null,
            gateway: InboundMediaGateway? = null,
            directSessionFactory: (() -> DirectCapableSession)? = directSession?.let { { it } },
            gatewayFactory: (StoredPairing) -> InboundMediaGateway = { gateway ?: error("gateway should not be used") },
            inviteProvider: suspend (StoredPairing) -> Invite,
        ): CallManager = CallManager(
            sessionFactory = { session },
            inviteProvider = inviteProvider,
            hostedSessionProvider = hostedSessionProvider,
            takeoverClaimProvider = takeoverClaimProvider,
            idempotencyKeyProvider = idempotencyKeyProvider,
            onForeground = { foreground += it },
            scope = scope,
            ioDispatcher = dispatcher,
            takeoverMediaTimeoutMs = takeoverMediaTimeoutMs,
            takeoverFailureVisibleMs = takeoverFailureVisibleMs,
            directSessionFactory = directSessionFactory,
            mediaGatewayFactory = gatewayFactory,
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
    // ---- #95 inbound takeover ----

    private fun takeoverSession() = HostedCallSession(
        sessionId = "claim_abcdefghijkl",
        livekitUrl = "wss://lk.example.com",
        token = "tok",
        expiresAt = 9_999_999_999_999,
    )

    @Test
    fun `answerTakeover happy path 不发 dial 且 connected 推进 InCall`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        val manager = h.manager(
            session,
            takeoverClaimProvider = { _, offerId ->
                assertEquals("offer_abcdefghijkl", offerId)
                takeoverSession()
            },
        ) { error("invite path should not run") }
        val hostedPairing = pairing.copy(protocol = PairingProtocol.HOSTED, edgeId = "edge_abcdefghijkl")

        manager.answerTakeover(hostedPairing, "offer_abcdefghijkl")
        runCurrent()

        assertTrue(session.connected)
        assertTrue(manager.state.value is CallState.WaitingMedia)
        assertTrue(session.commands.none { it.contains("\"dial\"") })

        session.emit(SessionEvent.Edge(Signaling.Event.Status("connected", null)))
        runCurrent()
        assertTrue(manager.state.value is CallState.InCall)

        advanceTimeBy(60_000)
        runCurrent()
        assertTrue(manager.state.value is CallState.InCall)
        h.close()
    }

    @Test
    fun `answerTakeover 他机已接或机主已拒绝按结束原因展示而不是失败`() = runTest {
        for ((code, reason) in listOf(
            "OFFER_ANSWERED_ELSEWHERE" to CallEndReasons.ANSWERED_ELSEWHERE,
            "OFFER_OWNER_DECLINED" to CallEndReasons.OWNER_DECLINED,
        )) {
            val h = Harness(this)
            val session = FakeSession()
            val manager = h.manager(
                session,
                takeoverClaimProvider = { _, _ -> throw HostedCloudException(409, code, "elsewhere") },
            ) { error("invite path should not run") }
            val hostedPairing = pairing.copy(protocol = PairingProtocol.HOSTED, edgeId = "edge_abcdefghijkl")

            manager.answerTakeover(hostedPairing, "offer_abcdefghijkl")
            runCurrent()

            val state = manager.state.value
            assertTrue("$code -> $state", state is CallState.Ended && state.reason == reason)
            assertFalse(session.connected)
            h.close()
        }
    }

    @Test
    fun `answerTakeover 其他 409 仍是失败并带错误码`() = runTest {
        val h = Harness(this)
        val manager = h.manager(
            FakeSession(),
            takeoverClaimProvider = { _, _ -> throw HostedCloudException(409, "OFFER_UNAVAILABLE", "gone") },
        ) { error("invite path should not run") }
        val hostedPairing = pairing.copy(protocol = PairingProtocol.HOSTED, edgeId = "edge_abcdefghijkl")

        manager.answerTakeover(hostedPairing, "offer_abcdefghijkl")
        runCurrent()

        val state = manager.state.value
        assertTrue("$state", state is CallState.Failed && state.code == "OFFER_UNAVAILABLE")
        h.close()
    }

    private fun v2Claim(sdpAnswer: String? = null) = HostedCallSession(
        sessionId = "claim_abcdefghijkl",
        livekitUrl = "wss://lk.example.com",
        token = "tok",
        expiresAt = 9_999_999_999_999,
        directAttempted = true,
        sdpPending = sdpAnswer == null,
        sdpAnswer = sdpAnswer,
    )

    private val hosted = pairing.copy(protocol = PairingProtocol.HOSTED, edgeId = "edge_abcdefghijkl")

    @Test
    fun `直连 happy path claim 后取 TURN 补 SDP 连通 再由 Edge connected 推进 InCall`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer typ relay"))
        val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }

        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()

        assertEquals(listOf("claim:true", "ice", "sdp"), gateway.calls)
        assertEquals("v=0\r\nanswer typ relay", direct.answer)
        assertFalse(direct.connected)
        assertTrue(manager.state.value is CallState.WaitingMedia)

        direct.emit(SessionEvent.Edge(Signaling.Event.Status("connected", null)))
        runCurrent()
        assertTrue(manager.state.value is CallState.InCall)

        manager.hangup()
        runCurrent()
        assertTrue(gateway.calls.contains("end"))
        assertTrue(direct.commands.any { it.contains("\"hangup\"") })
        h.close()
    }

    @Test
    fun `直连拿不到 answer 或连通失败时同 claim 回落 LiveKit`() = runTest {
        for (mode in listOf("no-answer", "connect-fails")) {
            val h = Harness(this)
            val direct = FakeDirectSession().apply { failConnect = mode == "connect-fails" }
            val sdp = if (mode == "no-answer") v2Claim().copy(sdpPending = false) else v2Claim(sdpAnswer = "v=0\r\nanswer")
            val gateway = FakeGateway(v2Claim(), sdpResult = sdp)
            val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }

            manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
            runCurrent()

            assertTrue("$mode ${gateway.calls}", gateway.calls.last() == "fallback")
            assertTrue(mode, direct.connected)
            assertTrue(mode, direct.closedDirect >= 0)
            assertTrue(mode, manager.state.value is CallState.WaitingMedia)
            h.close()
        }
    }

    @Test
    fun `直连提交前 media_failed 或断开触发回落 提交后断开才算结束`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer"))
        val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }
        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()
        assertFalse(gateway.calls.contains("fallback"))

        direct.emit(SessionEvent.Edge(Signaling.Event.MediaFailed("no_phone_audio")))
        runCurrent()
        assertEquals(1, gateway.calls.count { it == "fallback" })
        assertTrue(direct.connected)
        assertTrue(direct.closedDirect >= 1)
        // 旧对等连接的尾随断开事件与本通无关：不重复回落、也不结束通话。
        direct.emit(SessionEvent.Disconnected("direct_failed"))
        direct.emit(SessionEvent.Disconnected("channel_closed"))
        runCurrent()
        assertEquals(1, gateway.calls.count { it == "fallback" })
        assertTrue(manager.state.value is CallState.WaitingMedia)
        // 回落后的 LiveKit 房间断开才是真正结束。
        direct.emit(SessionEvent.Disconnected("disconnected"))
        runCurrent()
        assertTrue(manager.state.value is CallState.Ended)
        h.close()
    }

    @Test
    fun `直连准备阶段超时回落而不是等整体超时`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession().apply { prepareGate = CompletableDeferred() }
        val gateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer"))
        val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }
        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()
        assertEquals(listOf("claim:true", "ice"), gateway.calls)

        advanceTimeBy(20_001)
        runCurrent()

        assertEquals("fallback", gateway.calls.last())
        assertTrue(direct.connected)
        assertTrue(manager.state.value is CallState.WaitingMedia)
        h.close()
    }

    @Test
    fun `回落请求途中挂断 不再连房间`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer")).apply { fallbackGate = CompletableDeferred() }
        val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }
        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()
        direct.emit(SessionEvent.Edge(Signaling.Event.MediaFailed("no_phone_audio")))
        runCurrent()
        assertEquals("fallback", gateway.calls.last())

        manager.hangup()
        runCurrent()
        val ended = manager.state.value
        assertTrue("$ended", ended is CallState.Ended && ended.reason == "local_hangup")
        gateway.fallbackGate!!.complete(Unit)
        runCurrent()

        assertFalse(direct.connected)
        assertTrue(gateway.calls.contains("end"))
        h.close()
    }

    @Test
    fun `回落 connect 进行中挂断 会话被断开且不再推进`() = runTest {
        val h = Harness(this)
        lateinit var manager: CallManager
        val direct = object : FakeDirectSession() {
            override suspend fun connect(livekitUrl: String, token: String) {
                connected = true
                // 房间正在连的瞬间机主挂断：挂断收尾必须断掉这个刚起的回落会话。
                manager.hangup()
            }
        }
        val gateway = FakeGateway(v2Claim(), sdpResult = v2Claim().copy(sdpPending = false))
        manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }

        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()

        val ended = manager.state.value
        assertTrue("$ended", ended is CallState.Ended && ended.reason == "local_hangup")
        assertTrue(direct.connected)
        assertTrue(direct.disconnected)
        assertTrue(gateway.calls.contains("end"))
        direct.emit(SessionEvent.Edge(Signaling.Event.Status("connected", null)))
        runCurrent()
        assertTrue(manager.state.value is CallState.Ended)
        h.close()
    }

    @Test
    fun `上一通清理阻塞在断开时开始下一通 新通话的上下文不被旧清理清掉`() = runTest {
        val h = Harness(this)
        lateinit var manager: CallManager
        val second = FakeDirectSession()
        val secondGateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer"))
        var nextCall = 0
        val first = object : FakeDirectSession() {
            override fun disconnect() {
                super.disconnect()
                // 旧会话 disconnect 卡在 native 释放时，用户已从界面开始下一通。
                if (nextCall == 0) {
                    nextCall = 1
                    manager.reset()
                    manager.answerTakeover(hosted, "offer_abcdefghijkm", direct = true)
                }
            }
        }
        val firstGateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer"))
        val sessions = listOf<DirectCapableSession>(first, second)
        val gateways = listOf<InboundMediaGateway>(firstGateway, secondGateway)
        var created = 0
        manager = h.manager(
            FakeSession(),
            directSessionFactory = { sessions[created++] },
            gatewayFactory = { gateways[if (nextCall == 0) 0 else 1] },
        ) { error("invite path should not run") }

        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()
        assertTrue(manager.state.value is CallState.WaitingMedia)
        // 第一通媒体超时 → Failed → cleanup → disconnect 中开始第二通。
        advanceTimeBy(60_001)
        runCurrent()
        assertTrue(first.disconnected)

        // 第二通正常建连，回落与结束都仍找得到自己的上下文。
        assertTrue("${manager.state.value}", manager.state.value is CallState.WaitingMedia)
        assertEquals(listOf("claim:true", "ice", "sdp"), secondGateway.calls)
        second.emit(SessionEvent.Edge(Signaling.Event.MediaFailed("no_phone_audio")))
        runCurrent()
        assertEquals("fallback", secondGateway.calls.last())
        assertTrue(second.connected)
        manager.hangup()
        runCurrent()
        assertTrue("${secondGateway.calls}", secondGateway.calls.contains("end"))
        assertFalse(firstGateway.calls.contains("fallback"))
        h.close()
    }

    @Test
    fun `看到媒体超时 Failed 立刻开始的下一通 不被超时清理碰到`() = runTest {
        val h = Harness(this)
        lateinit var manager: CallManager
        val first = FakeDirectSession()
        val second = FakeDirectSession()
        val firstGateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer"))
        val secondGateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer"))
        var created = 0
        var calls = 0
        manager = h.manager(
            FakeSession(),
            directSessionFactory = { listOf<DirectCapableSession>(first, second)[created++] },
            gatewayFactory = { listOf<InboundMediaGateway>(firstGateway, secondGateway)[calls++] },
        ) { error("invite path should not run") }
        // 界面一看到 Failed 就返回并接下一通（与真机上用户的最快操作一致）。
        val watcher = h.scope.launch {
            manager.state.collect { state ->
                if (state is CallState.Failed && state.code == "TAKEOVER_MEDIA_TIMEOUT") {
                    manager.reset()
                    manager.answerTakeover(hosted, "offer_abcdefghijkm", direct = true)
                }
            }
        }

        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()
        advanceTimeBy(60_001)
        runCurrent()

        assertTrue(first.disconnected)
        assertTrue("${manager.state.value}", manager.state.value is CallState.WaitingMedia)
        assertEquals(listOf("claim:true", "ice", "sdp"), secondGateway.calls)
        assertFalse(second.disconnected)
        second.emit(SessionEvent.Edge(Signaling.Event.MediaFailed("no_phone_audio")))
        runCurrent()
        assertEquals("fallback", secondGateway.calls.last())
        watcher.cancel()
        h.close()
    }

    @Test
    fun `claim 在挂断之后才成功也要通知中转结束`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeGateway(v2Claim()).apply { claimGate = CompletableDeferred() }
        val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }
        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()
        assertEquals(listOf("claim:true"), gateway.calls)

        manager.hangup()
        runCurrent()
        assertFalse(gateway.calls.contains("end"))
        gateway.claimGate!!.complete(Unit)
        runCurrent()

        assertTrue("${gateway.calls}", gateway.calls.contains("end"))
        assertTrue(direct.prepared.isEmpty())
        h.close()
    }

    @Test
    fun `直连提交后 Edge ended 通知直接结束通话`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer"))
        val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }
        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()
        direct.emit(SessionEvent.Edge(Signaling.Event.Status("connected", null)))
        runCurrent()
        assertTrue(manager.state.value is CallState.InCall)

        direct.emit(SessionEvent.Edge(Signaling.Event.Ended("call_ended")))
        runCurrent()

        val state = manager.state.value
        assertTrue("$state", state is CallState.Ended && state.reason == "call_ended")
        assertTrue(direct.disconnected)
        h.close()
    }

    @Test
    fun `中转没走直连流程时即便开了直连也用 LiveKit 会话`() = runTest {
        val h = Harness(this)
        val legacy = FakeSession()
        val direct = FakeDirectSession()
        val gateway = FakeGateway(v2Claim().copy(directAttempted = false, sdpPending = false))
        val manager = h.manager(legacy, directSession = direct, gateway = gateway) { error("invite path should not run") }

        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()

        assertEquals(listOf("claim:true"), gateway.calls)
        assertTrue(legacy.connected)
        assertTrue(direct.prepared.isEmpty())
        h.close()
    }

    @Test
    fun `answerTakeover connect 期间 connected 到达不被覆盖回等待态`() = runTest {
        val h = Harness(this)
        val session = object : FakeSession() {
            override suspend fun connect(livekitUrl: String, token: String) {
                // 接管场景物理通话已在进行：connected 可能在 connect 内就绪。
                emit(SessionEvent.Edge(Signaling.Event.Status("connected", null)))
                super.connect(livekitUrl, token)
            }
        }
        val manager = h.manager(
            session,
            takeoverClaimProvider = { _, _ -> takeoverSession() },
        ) { error("invite path should not run") }
        val hostedPairing = pairing.copy(protocol = PairingProtocol.HOSTED, edgeId = "edge_abcdefghijkl")

        manager.answerTakeover(hostedPairing, "offer_abcdefghijkl")
        runCurrent()

        assertTrue(manager.state.value is CallState.InCall)
        h.close()
    }

    @Test
    fun `answerTakeover 等待媒体超时后清理并自动回到 Idle`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        val manager = h.manager(
            session,
            takeoverClaimProvider = { _, _ -> takeoverSession() },
            takeoverMediaTimeoutMs = 1_000L,
            takeoverFailureVisibleMs = 500L,
        ) { error("invite path should not run") }
        val hostedPairing = pairing.copy(protocol = PairingProtocol.HOSTED, edgeId = "edge_abcdefghijkl")

        manager.answerTakeover(hostedPairing, "offer_abcdefghijkl")
        runCurrent()
        assertEquals(CallState.WaitingMedia("Incoming call"), manager.state.value)

        advanceTimeBy(999L)
        runCurrent()
        assertEquals(CallState.WaitingMedia("Incoming call"), manager.state.value)

        advanceTimeBy(1L)
        runCurrent()
        assertEquals(
            CallState.Failed("Incoming call", "接管媒体建立超时", "TAKEOVER_MEDIA_TIMEOUT"),
            manager.state.value,
        )
        assertTrue(session.disconnected)
        assertEquals(listOf(true, false), h.foreground)

        advanceTimeBy(500L)
        runCurrent()
        assertEquals(CallState.Idle, manager.state.value)
        h.close()
    }

    @Test
    fun `answerTakeover claim 失败落结构化错误`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        val manager = h.manager(
            session,
            takeoverClaimProvider = { _, _ ->
                throw HostedCloudException(409, "OFFER_UNAVAILABLE", "already claimed")
            },
        ) { error("invite path should not run") }
        val hostedPairing = pairing.copy(protocol = PairingProtocol.HOSTED, edgeId = "edge_abcdefghijkl")

        manager.answerTakeover(hostedPairing, "offer_abcdefghijkl")
        advanceUntilIdle()

        val state = manager.state.value
        assertTrue(state is CallState.Failed)
        assertEquals("OFFER_UNAVAILABLE", (state as CallState.Failed).code)
        assertFalse(session.connected)
        h.close()
    }
}
