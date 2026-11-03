package ai.bondings.callpilot.call

import ai.bondings.callpilot.media.DirectCapableSession
import ai.bondings.callpilot.media.RemoteSession
import ai.bondings.callpilot.media.SessionEvent
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.DeviceCredential
import ai.bondings.callpilot.protocol.HostedCallSession
import ai.bondings.callpilot.protocol.HostedCloudException
import ai.bondings.callpilot.protocol.IceServer
import ai.bondings.callpilot.protocol.InboundOffer
import ai.bondings.callpilot.protocol.OutboundCall
import ai.bondings.callpilot.protocol.Invite
import ai.bondings.callpilot.protocol.PairingProtocol
import ai.bondings.callpilot.protocol.Signaling
import java.util.Base64
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext
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

        var connectedToken: String? = null

        override suspend fun connect(livekitUrl: String, token: String) {
            connected = true
            connectedToken = token
        }

        /** 客户端里程碑（client_timing）单独记，不混进业务命令。 */
        val timings = mutableListOf<String>()

        override suspend fun sendCommand(json: String) {
            if (json.contains("\"client_timing\"")) {
                timings += json
                return
            }
            if (json.contains("\"dial\"")) {
                dialStarted.complete(Unit)
                dialRelease?.await()
            }
            commands += json
            if (autoAckHangup && json.contains("\"hangup\"")) {
                // 与真实 Edge 一致：收到带编号的挂断就回同一编号的确认。
                val id = (kotlinx.serialization.json.Json.parseToJsonElement(json) as kotlinx.serialization.json.JsonObject)["commandId"]
                    .toString().trim('"')
                _events.tryEmit(SessionEvent.Edge(Signaling.Event.HangupAck(id)))
            }
        }

        /** 真实 Edge 会回 hangup.ack；个别测试关掉它来验证超时重发。 */
        var autoAckHangup = true

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
        var fallbackError: Exception? = null
        var claimedWithSdp: String? = null
        override suspend fun claim(offerId: String, direct: Boolean, sdpOffer: String?): HostedCallSession {
            calls += if (sdpOffer != null) "claim:$direct+sdp" else "claim:$direct"
            claimedWithSdp = sdpOffer
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

        var fallbackTiming: List<String> = emptyList()
        var endTiming: List<String> = emptyList()

        override suspend fun fallbackLiveKit(offerId: String, claimId: String, clientTiming: List<String>): HostedCallSession {
            fallbackTiming = clientTiming
            calls += "fallback"
            withContext(NonCancellable) {
                fallbackGate?.await()
                fallbackError?.let { throw it }
            }
            return claimResult.copy(token = "fallback-token", directAttempted = true, sdpPending = false)
        }

        override suspend fun end(offerId: String, claimId: String, clientTiming: List<String>) {
            endTiming = clientTiming
            calls += "end"
        }
    }

    private class FakeOutboundGateway(
        var created: OutboundCall = OutboundCall("call_abcdefghijkl", 9_999_999L, true, "v=0\r\nedge-answer"),
    ) : OutboundMediaGateway {
        val calls = mutableListOf<String>()
        var iceError: Exception? = null
        var createError: Exception? = null
        var createGate: CompletableDeferred<Unit>? = null
        var fallbackGate: CompletableDeferred<Unit>? = null
        var fallbackError: Exception? = null
        var offer: String? = null

        override suspend fun iceServers(edgeId: String): List<IceServer> {
            calls += "ice"
            iceError?.let { throw it }
            return listOf(IceServer(listOf("turn:turn.example:3478"), "u", "c"))
        }

        override suspend fun create(edgeId: String, idempotencyKey: String, sdpOffer: String): OutboundCall {
            calls += "create"
            offer = sdpOffer
            createGate?.await()
            createError?.let { throw it }
            return created
        }

        override suspend fun awaitSession(edgeId: String, call: OutboundCall): HostedCallSession {
            calls += "poll"
            return HostedCallSession(call.callId, "wss://lk.example.com", "polled-token", 9_999_999L)
        }

        override suspend fun fallbackLiveKit(callId: String): HostedCallSession {
            calls += "fallback:$callId"
            withContext(NonCancellable) {
                fallbackGate?.await()
                fallbackError?.let { throw it }
            }
            return HostedCallSession(callId, "wss://lk.example.com", "fallback-token", 9_999_999L)
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
    private class Harness(private val testScope: TestScope) {
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
            outboundGateway: OutboundMediaGateway? = null,
            outboundDirectProvider: (StoredPairing) -> Boolean = { outboundGateway != null },
            clockMs: () -> Long = { 1_000L },
            timingClockMs: () -> Double = { testScope.testScheduler.currentTime.toDouble() },
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
            outboundGatewayFactory = { outboundGateway ?: error("outbound gateway should not be used") },
            outboundDirectProvider = outboundDirectProvider,
            clockMs = clockMs,
            timingClockMs = timingClockMs,
        )

        fun close() = scope.cancel()
    }

    @Test
    fun `cached capability routes immediately without waiting for refresh`() = runTest {
        for (warm in listOf(true, false)) {
            val h = Harness(this)
            var requests = 0
            val pendingRefresh = CompletableDeferred<ai.bondings.callpilot.protocol.HostedDeviceStatus?>()
            val status = ai.bondings.callpilot.protocol.HostedDeviceStatus(
                false, null, kotlinx.serialization.json.JsonObject(emptyMap()),
                kotlinx.serialization.json.Json.parseToJsonElement("""{"outboundDirectV1":true,"inboundFastpathV2":true}""") as kotlinx.serialization.json.JsonObject,
            )
            val cache = DirectCapabilityCache(h.scope) {
                requests++
                if (requests == 1) {
                    if (warm) status else null
                } else pendingRefresh.await()
            }
            cache.activate(hosted)
            runCurrent()
            assertEquals(1, requests)
            val legacy = FakeSession()
            val direct = FakeDirectSession()
            val manager = h.manager(
                legacy,
                hostedSessionProvider = { _, _ -> HostedCallSession("call_abcdefghijkl", "wss://lk.example.com", "legacy-token", 9_999_999L) },
                directSession = direct,
                outboundGateway = FakeOutboundGateway(),
                outboundDirectProvider = { cache.cached(it).outboundDirectV1 },
            ) { error("no tunnel") }
            manager.startCall(hosted, "10086")
            runCurrent()
            assertEquals(warm, direct.prepared.isNotEmpty())
            assertEquals(!warm, legacy.connected)
            assertEquals(if (warm) 1 else 2, requests)
            assertFalse(pendingRefresh.isCompleted)
            h.close()
        }
    }

    @Test
    fun `outbound server capability true selects direct otherwise LiveKit`() = runTest {
        for (value in listOf("true", "false", "null", "\"true\"", "missing", "unavailable")) {
            val h = Harness(this)
            val legacy = FakeSession()
            val direct = FakeDirectSession()
            val features = kotlinx.serialization.json.Json.parseToJsonElement(
                if (value in listOf("missing", "unavailable")) "{}" else """{"outboundDirectV1":$value}""",
            ) as kotlinx.serialization.json.JsonObject
            val status = ai.bondings.callpilot.protocol.HostedDeviceStatus(
                false, null, kotlinx.serialization.json.JsonObject(emptyMap()), features,
            )
            val manager = h.manager(
                legacy,
                hostedSessionProvider = { _, _ ->
                    HostedCallSession("call_abcdefghijkl", "wss://lk.example.com", "legacy-token", 9_999_999L)
                },
                directSession = direct,
                outboundGateway = FakeOutboundGateway(),
                outboundDirectProvider = {
                    if (value == "unavailable") error("device status unavailable")
                    status.outboundDirectV1
                },
            ) { error("no tunnel") }
            manager.startCall(hosted, "10086")
            runCurrent()
            assertEquals(value, value == "true", direct.prepared.isNotEmpty())
            assertEquals(value, value != "true", legacy.connected)
            h.close()
        }
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
            SessionEvent.Edge(Signaling.decodeEvent(
                """{"type":"status","status":"failed","reason":"line_unavailable","code":"SIM_NOT_REGISTERED"}"""
            )!!)
        )
        advanceUntilIdle()
        assertEquals(CallState.Failed("10000", "line_unavailable", "SIM_NOT_REGISTERED"), manager.state.value)
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

    private val ringing = InboundOffer("offer_abcdefghijkl", 16_000L)

    @Test
    fun `响铃期预备好的 SDP 随 claim 发出 answer 直接连通 不再补交`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeGateway(v2Claim(sdpAnswer = "v=0\r\nanswer"))
        val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }

        manager.prepareTakeover(hosted, ringing)
        runCurrent()
        assertEquals(listOf("ice"), gateway.calls)
        assertEquals(1, direct.prepared.size)

        manager.answerTakeover(hosted, ringing.offerId, direct = true)
        runCurrent()

        assertEquals(listOf("ice", "claim:true+sdp"), gateway.calls)
        assertEquals("v=0\r\nfake-offer typ relay", gateway.claimedWithSdp)
        assertEquals("v=0\r\nanswer", direct.answer)
        assertEquals(1, direct.prepared.size) // the peer prepared while ringing is the one used
        h.close()
    }

    @Test
    fun `预备还没收集完就接听 claim 先定归属 再用同一份预备补交`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession().apply { prepareGate = CompletableDeferred() }
        val gateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer"))
        val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }
        manager.prepareTakeover(hosted, ringing)
        runCurrent()

        manager.answerTakeover(hosted, ringing.offerId, direct = true)
        runCurrent()
        assertEquals(listOf("ice", "claim:true"), gateway.calls)
        direct.prepareGate!!.complete(Unit)
        runCurrent()

        assertEquals(listOf("ice", "claim:true", "sdp"), gateway.calls)
        assertEquals(1, direct.prepared.size)
        assertEquals("v=0\r\nanswer", direct.answer)
        h.close()
    }

    @Test
    fun `没在本机接听或过期就放掉预备的连接`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeGateway(v2Claim())
        val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }
        manager.prepareTakeover(hosted, ringing)
        runCurrent()
        manager.discardPrepared("offer_zzzzzzzzzzzz") // other offers are not touched
        assertFalse(direct.disconnected)
        manager.discardPrepared(ringing.offerId)
        assertTrue(direct.disconnected)

        val second = FakeDirectSession()
        val manager2 = h.manager(FakeSession(), directSession = second, gateway = gateway,
            clockMs = { 15_000L }) { error("invite path should not run") }
        manager2.prepareTakeover(hosted, ringing)
        runCurrent()
        advanceTimeBy(3_100) // offer expires at 16 s + 2 s grace, clock says 15 s
        runCurrent()
        assertTrue(second.disconnected)
        h.close()
    }

    @Test
    fun `中转没走直连时放掉预备 改用 LiveKit 会话`() = runTest {
        val h = Harness(this)
        val legacy = FakeSession()
        val direct = FakeDirectSession()
        val gateway = FakeGateway(v2Claim().copy(directAttempted = false, sdpPending = false))
        val manager = h.manager(legacy, directSession = direct, gateway = gateway) { error("invite path should not run") }
        manager.prepareTakeover(hosted, ringing)
        runCurrent()

        manager.answerTakeover(hosted, ringing.offerId, direct = true)
        runCurrent()

        assertTrue(direct.disconnected)
        assertTrue(legacy.connected)
        h.close()
    }

    @Test
    fun `预备被接听取走后 claim 失败或 claim 途中挂断都会放掉它`() = runTest {
        for (mode in listOf("claim-fails", "hangup-during-claim")) {
            val h = Harness(this)
            val direct = FakeDirectSession()
            val gateway = object : InboundMediaGateway by FakeGateway(v2Claim()) {
                val gate = CompletableDeferred<Unit>()
                override suspend fun claim(offerId: String, direct: Boolean, sdpOffer: String?): HostedCallSession {
                    if (mode == "claim-fails") throw HostedCloudException(409, "OFFER_UNAVAILABLE", "gone")
                    gate.await()
                    return v2Claim()
                }
            }
            val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }
            manager.prepareTakeover(hosted, ringing)
            runCurrent()
            manager.answerTakeover(hosted, ringing.offerId, direct = true)
            runCurrent()
            if (mode == "hangup-during-claim") {
                manager.hangup()
                runCurrent()
            }
            assertTrue(mode, direct.disconnected)
            h.close()
        }
    }

    @Test
    fun `预备失败时接听照常走补交流程的回落`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession().apply { failPrepare = true }
        val gateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer"))
        val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }
        manager.prepareTakeover(hosted, ringing)
        runCurrent()
        manager.answerTakeover(hosted, ringing.offerId, direct = true)
        runCurrent()
        assertEquals(listOf("ice", "claim:true", "fallback"), gateway.calls)
        assertTrue(direct.connected)
        h.close()
    }

    @Test
    fun `预备在接听前或接听后超时 都只回落一次且不重复预备`() = runTest {
        for (answerAfterTimeout in listOf(false, true)) {
            val h = Harness(this)
            val direct = FakeDirectSession().apply { prepareGate = CompletableDeferred() } // gathering never finishes
            val gateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer"))
            val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }
            val later = InboundOffer("offer_abcdefghijkl", 120_000L)
            manager.prepareTakeover(hosted, later)
            runCurrent()
            if (answerAfterTimeout) {
                advanceTimeBy(20_001)
                runCurrent()
            }
            manager.answerTakeover(hosted, later.offerId, direct = true)
            runCurrent()
            advanceTimeBy(20_001)
            runCurrent()
            assertEquals("timeout after=$answerAfterTimeout ${gateway.calls}", 1, gateway.calls.count { it == "fallback" })
            assertEquals(1, direct.prepared.size)
            assertFalse(gateway.calls.contains("sdp"))
            h.close()
        }
    }

    @Test
    fun `上一通清理阻塞期间为下一通预备并接听 新预备不被旧清理碰到`() = runTest {
        val h = Harness(this)
        lateinit var manager: CallManager
        val second = FakeDirectSession()
        val secondGateway = FakeGateway(v2Claim(sdpAnswer = "v=0\r\nanswer"))
        val next = InboundOffer("offer_abcdefghijkm", 120_000L)
        var started = false
        val first = object : FakeDirectSession() {
            override fun disconnect() {
                super.disconnect()
                if (!started) {
                    started = true
                    manager.reset()
                    manager.prepareTakeover(hosted, next)
                    manager.answerTakeover(hosted, next.offerId, direct = true)
                }
            }
        }
        val firstGateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer"))
        val sessions = listOf<DirectCapableSession>(first, second)
        var created = 0
        var gateways = 0
        manager = h.manager(
            FakeSession(),
            directSessionFactory = { sessions[created++] },
            gatewayFactory = { listOf<InboundMediaGateway>(firstGateway, secondGateway)[gateways++] },
        ) { error("invite path should not run") }

        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()
        advanceTimeBy(60_001) // first call's media timeout → cleanup → disconnect starts the next call
        runCurrent()

        assertTrue(first.disconnected)
        assertFalse(second.disconnected)
        assertEquals("v=0\r\nanswer", second.answer)
        assertEquals(listOf("ice", "claim:true+sdp"), secondGateway.calls)
        manager.hangup()
        runCurrent()
        assertTrue(second.disconnected)
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


    @Test
    fun `外呼直连 带 offer 建呼叫后用 answer 连通 media_ready 后经直连发 dial`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeOutboundGateway()
        val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = gateway) { error("no tunnel") }

        manager.startCall(hosted, "10086")
        runCurrent()

        assertEquals(listOf("ice", "create"), gateway.calls)
        assertEquals("v=0\r\nfake-offer typ relay", gateway.offer)
        assertEquals("v=0\r\nedge-answer", direct.answer)
        assertEquals(CallState.WaitingMedia("10086"), manager.state.value)
        direct.emit(SessionEvent.Edge(Signaling.Event.Status("media_ready")))
        runCurrent()
        assertTrue(direct.commands.single().contains("10086"))
        assertFalse(direct.connected)
        h.close()
    }

    @Test
    fun `外呼直连 不可用时照旧走 LiveKit 不建直连`() = runTest {
        val h = Harness(this)
        val legacy = FakeSession()
        val direct = FakeDirectSession()
        val gateway = FakeOutboundGateway().also { it.iceError = HostedCloudException(503, "DIRECT_UNAVAILABLE", "off") }
        val manager = h.manager(
            legacy, directSession = direct, outboundGateway = gateway,
            hostedSessionProvider = { _, _ -> HostedCallSession("call_abcdefghijkl", "wss://lk.example.com", "legacy-token", 9_999_999L) },
        ) { error("no tunnel") }

        manager.startCall(hosted, "10086")
        runCurrent()

        assertEquals(listOf("ice"), gateway.calls)
        assertTrue(direct.prepared.isEmpty())
        assertEquals("legacy-token", legacy.connectedToken)
        assertEquals(CallState.WaitingMedia("10086"), manager.state.value)
        h.close()
    }

    @Test
    fun `外呼直连 收集 offer 失败时放掉对等连接改走 LiveKit`() = runTest {
        val h = Harness(this)
        val legacy = FakeSession()
        val direct = FakeDirectSession().also { it.failPrepare = true }
        val gateway = FakeOutboundGateway()
        val manager = h.manager(
            legacy, directSession = direct, outboundGateway = gateway,
            hostedSessionProvider = { _, _ -> HostedCallSession("call_abcdefghijkl", "wss://lk.example.com", "legacy-token", 9_999_999L) },
        ) { error("no tunnel") }

        manager.startCall(hosted, "10086")
        runCurrent()

        assertEquals(listOf("ice"), gateway.calls)
        assertTrue(direct.disconnected)
        assertEquals("legacy-token", legacy.connectedToken)
        h.close()
    }

    @Test
    fun `外呼直连 连不上时同一通回落 LiveKit 只回落一次`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession().also { it.failConnect = true }
        val gateway = FakeOutboundGateway()
        val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = gateway) { error("no tunnel") }

        manager.startCall(hosted, "10086")
        runCurrent()
        direct.emit(SessionEvent.Disconnected("direct_failed"))
        runCurrent()

        assertEquals(listOf("ice", "create", "fallback:call_abcdefghijkl"), gateway.calls)
        assertEquals(1, direct.closedDirect)
        assertEquals("fallback-token", direct.connectedToken)
        assertEquals(CallState.WaitingMedia("10086"), manager.state.value)
        h.close()
    }

    @Test
    fun `外呼直连 Edge 没建成直连时直接回落 中转没走直连时照旧轮询`() = runTest {
        for ((created, expected, token) in listOf(
            Triple(OutboundCall("call_abcdefghijkl", 9_999_999L, true, null), "fallback:call_abcdefghijkl", "fallback-token"),
            Triple(OutboundCall("call_abcdefghijkl", 9_999_999L, false, null), "poll", "polled-token"),
        )) {
            val h = Harness(this)
            val direct = FakeDirectSession()
            val gateway = FakeOutboundGateway(created)
            val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = gateway) { error("no tunnel") }
            manager.startCall(hosted, "10086")
            runCurrent()
            assertEquals(listOf("ice", "create", expected), gateway.calls)
            assertEquals(null, direct.answer)
            assertEquals(1, direct.closedDirect)
            assertEquals(token, direct.connectedToken)
            h.close()
        }
    }

    @Test
    fun `外呼直连 拨号后直连断开按结束处理不回落`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeOutboundGateway()
        val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = gateway) { error("no tunnel") }
        manager.startCall(hosted, "10086")
        runCurrent()
        direct.emit(SessionEvent.Edge(Signaling.Event.Status("dialing")))
        runCurrent()

        direct.emit(SessionEvent.Disconnected("direct_failed"))
        runCurrent()

        assertEquals(listOf("ice", "create"), gateway.calls)
        assertEquals(CallState.Ended("10086", "direct_failed"), manager.state.value)
        assertTrue(direct.disconnected)
        h.close()
    }

    @Test
    fun `外呼直连 建呼叫被拒绝时显示失败码并释放连接`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeOutboundGateway().also { it.createError = HostedCloudException(200, "LINE_BUSY", "云端呼叫创建失败") }
        val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = gateway) { error("no tunnel") }

        manager.startCall(hosted, "10086")
        runCurrent()

        val state = manager.state.value as CallState.Failed
        assertEquals("LINE_BUSY", state.code)
        assertTrue(direct.disconnected)
        assertEquals(listOf(true, false), h.foreground)
        h.close()
    }

    @Test
    fun `外呼直连 建呼叫期间挂断不连通也不回落`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeOutboundGateway().also { it.createGate = CompletableDeferred() }
        val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = gateway) { error("no tunnel") }
        manager.startCall(hosted, "10086")
        runCurrent()
        assertEquals(listOf("ice", "create"), gateway.calls)

        manager.hangup()
        gateway.createGate!!.complete(Unit)
        runCurrent()

        assertEquals(CallState.Ended("10086", "local_hangup"), manager.state.value)
        assertEquals(null, direct.answer)
        assertEquals(listOf("ice", "create"), gateway.calls)
        assertTrue(direct.disconnected)
        h.close()
    }

    @Test
    fun `外呼直连 dial 已交出但 Edge 确认未到时断开按结束处理不回落`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeOutboundGateway()
        val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = gateway) { error("no tunnel") }
        manager.startCall(hosted, "10086")
        runCurrent()
        direct.emit(SessionEvent.Edge(Signaling.Event.Status("media_ready")))
        runCurrent()
        assertTrue(direct.commands.single().contains("10086"))
        assertEquals(CallState.WaitingMedia("10086"), manager.state.value)

        direct.emit(SessionEvent.Disconnected("direct_failed"))
        runCurrent()

        assertEquals(listOf("ice", "create"), gateway.calls)
        assertEquals(CallState.Ended("10086", "direct_failed"), manager.state.value)
        h.close()
    }

    @Test
    fun `外呼直连 Edge 收不到手机音频时拨号前同一通回落`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeOutboundGateway()
        val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = gateway) { error("no tunnel") }
        manager.startCall(hosted, "10086")
        runCurrent()

        direct.emit(SessionEvent.Edge(Signaling.Event.MediaFailed("no_phone_audio")))
        runCurrent()

        assertEquals(listOf("ice", "create", "fallback:call_abcdefghijkl"), gateway.calls)
        assertEquals("fallback-token", direct.connectedToken)
        assertEquals(CallState.WaitingMedia("10086"), manager.state.value)
        h.close()
    }

    private fun sentMarks(session: FakeSession): Set<String> = session.timings.flatMap { batch ->
        kotlinx.serialization.json.Json.parseToJsonElement(batch)
            .let { (it as kotlinx.serialization.json.JsonObject)["marks"] as kotlinx.serialization.json.JsonObject }.keys
    }.toSet()

    @Test
    fun `外呼直连把客户端里程碑经控制通道交给 Edge 且不混进业务命令`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = FakeOutboundGateway()) { error("no tunnel") }
        manager.startCall(hosted, "10086")
        runCurrent()
        assertTrue(sentMarks(direct).containsAll(listOf("dial_start", "prepare_start", "prepare_response",
            "preconnect_start", "preconnect_ready", "claim_start", "claim_response", "connect_start", "connect_return",
            "direct_control_ready")))
        assertTrue(direct.commands.isEmpty())

        direct.emit(SessionEvent.Edge(Signaling.Event.Status("media_ready")))
        runCurrent()
        direct.emit(SessionEvent.Edge(Signaling.Event.Status("connected")))
        runCurrent()
        assertTrue("edge_connected" in sentMarks(direct))
        assertTrue(direct.commands.single().contains("10086"))

        manager.hangup()
        runCurrent()
        assertTrue("hangup_intent" in sentMarks(direct))
        assertTrue(direct.commands.last().contains("\"hangup\""))
        val seqs = direct.timings.map {
            (kotlinx.serialization.json.Json.parseToJsonElement(it) as kotlinx.serialization.json.JsonObject)["seq"].toString().toInt()
        }
        assertEquals(seqs.sorted(), seqs)
        assertEquals(seqs.toSet().size, seqs.size)
        h.close()
    }

    @Test
    fun `LiveKit 外呼也上报拨号与入房里程碑`() = runTest {
        val h = Harness(this)
        val session = FakeSession()
        val manager = h.manager(session) { invite() }
        manager.startCall(pairing, "10000")
        advanceUntilIdle()
        assertTrue(sentMarks(session).containsAll(listOf("dial_start", "connect_start", "connect_return")))
        assertTrue(session.commands.isEmpty())
        h.close()
    }

    @Test
    fun `来电直连接管的里程碑分段 SDP 补交与连接成对`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession()
        val gateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer typ relay"))
        val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }

        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()

        assertTrue(sentMarks(direct).containsAll(listOf("answer_action", "claim_start", "claim_response",
            "sdp_submit_start", "sdp_submit_response", "connect_start", "connect_return", "direct_control_ready")))
        h.close()
    }

    /** 首批发送挂起时，后到的补发必须排队，不能抢先送出更大的 seq（Edge 只收更大的 seq）。 */
    private class GatedTimingSession : FakeDirectSession() {
        val gate = CompletableDeferred<Unit>()
        val order = mutableListOf<Int>()
        override suspend fun sendCommand(json: String) {
            if (json.contains("\"client_timing\"")) {
                val seq = kotlinx.serialization.json.Json.parseToJsonElement(json)
                    .let { (it as kotlinx.serialization.json.JsonObject)["seq"].toString().toInt() }
                if (seq == 1) gate.await()
                order += seq
                timings += json
                return
            }
            super.sendCommand(json)
        }
    }

    @Test
    fun `里程碑补发串行 挂起的首批不会被后发的批次越过`() = runTest {
        val h = Harness(this)
        val direct = GatedTimingSession()
        val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = FakeOutboundGateway()) { error("no tunnel") }
        manager.startCall(hosted, "10086")
        runCurrent()
        direct.emit(SessionEvent.Edge(Signaling.Event.Status("media_ready")))
        runCurrent()
        direct.emit(SessionEvent.Edge(Signaling.Event.Status("connected")))
        runCurrent()
        assertTrue(direct.order.isEmpty())

        direct.gate.complete(Unit)
        runCurrent()
        assertEquals(direct.order.sorted(), direct.order)
        assertTrue("edge_connected" in sentMarks(direct))
        h.close()
    }

    @Test
    fun `里程碑发送卡住时挂断仍在上限内完成`() = runTest {
        val h = Harness(this)
        val direct = GatedTimingSession()
        val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = FakeOutboundGateway()) { error("no tunnel") }
        manager.startCall(hosted, "10086")
        runCurrent()

        manager.hangup()
        runCurrent()
        advanceTimeBy(299)
        runCurrent()
        assertTrue(direct.commands.none { it.contains("\"hangup\"") })

        advanceTimeBy(2)
        runCurrent()
        assertEquals(CallState.Ended("10086", "local_hangup"), manager.state.value)
        assertTrue(direct.commands.last().contains("\"hangup\""))
        h.close()
    }

    @Test
    fun `回落成功时补齐 connect 起止 让 Edge 能算出连接耗时`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession().also { it.failConnect = true }
        val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = FakeOutboundGateway()) { error("no tunnel") }
        manager.startCall(hosted, "10086")
        runCurrent()
        assertEquals("fallback-token", direct.connectedToken)
        assertTrue(sentMarks(direct).containsAll(listOf("connect_start", "connect_return", "cold_fallback", "room_connected")))
        h.close()
    }

    @Test
    fun `来电直连回落与结束接管时把里程碑随请求补交给中转`() = runTest {
        val h = Harness(this)
        val direct = FakeDirectSession().also { it.failConnect = true }
        val gateway = FakeGateway(v2Claim(), sdpResult = v2Claim(sdpAnswer = "v=0\r\nanswer typ relay"))
        val manager = h.manager(FakeSession(), directSession = direct, gateway = gateway) { error("invite path should not run") }

        manager.answerTakeover(hosted, "offer_abcdefghijkl", direct = true)
        runCurrent()
        assertTrue(gateway.calls.contains("fallback"))
        val fallbackMarks = gateway.fallbackTiming.flatMap {
            (kotlinx.serialization.json.Json.parseToJsonElement(it) as kotlinx.serialization.json.JsonObject)["marks"]
                .let { marks -> (marks as kotlinx.serialization.json.JsonObject).keys }
        }
        assertTrue(fallbackMarks.containsAll(listOf("answer_action", "claim_start", "claim_response", "cold_fallback")))

        manager.hangup()
        runCurrent()
        assertTrue(gateway.calls.contains("end"))
        assertTrue(gateway.endTiming.isNotEmpty())
        h.close()
    }

    private fun hangupIds(session: FakeSession): List<String> = session.commands.filter { it.contains("\"hangup\"") }.map {
        (kotlinx.serialization.json.Json.parseToJsonElement(it) as kotlinx.serialization.json.JsonObject)["commandId"]
            .toString().trim('"')
    }

    @Test
    fun `遥测与确认共用点击起算的1500毫秒预算`() = runTest {
        val h = Harness(this)
        val direct = GatedTimingSession().also { it.autoAckHangup = false }
        val manager = h.manager(FakeSession(), directSession = direct, outboundGateway = FakeOutboundGateway()) { error("no tunnel") }
        manager.startCall(hosted, "10086")
        runCurrent()
        manager.hangup()
        advanceTimeBy(1_499)
        runCurrent()
        assertFalse(direct.disconnected)
        assertEquals(2, hangupIds(direct).size)
        advanceTimeBy(1)
        runCurrent()
        assertTrue(direct.disconnected)
        assertEquals(CallState.Ended("10086", "local_hangup"), manager.state.value)
        h.close()
    }

    @Test
    fun `挂断协程延迟调度也不重置点击预算`() = runTest {
        val h = Harness(this)
        var monotonicMs = 0.0
        val session = FakeSession().also { it.autoAckHangup = false }
        val manager = h.manager(session, timingClockMs = { monotonicMs }) { invite() }
        manager.startCall(pairing, "10000")
        runCurrent()
        // 挂断登记同步完成，但挂断协程排队期间也必须消耗总预算。
        manager.hangup()
        monotonicMs = 1_500.0
        runCurrent()
        assertTrue(session.disconnected)
        h.close()
    }

    @Test
    fun `来电与外呼回落挂起后挂断 迟到失败不得结束本通或伤及下一通`() = runTest {
        for ((inbound, failAfterNextCall) in listOf(false to false, true to false, false to true, true to true)) {
            val h = Harness(this)
            val legacy = FakeSession()
            val direct = FakeDirectSession().also { it.autoAckHangup = false }
            val gate = CompletableDeferred<Unit>()
            val incoming = FakeGateway(v2Claim(sdpAnswer = "v=0\r\nanswer")).also {
                it.fallbackGate = gate
                it.fallbackError = IllegalStateException("late fallback failure")
            }
            val outgoing = FakeOutboundGateway().also {
                it.fallbackGate = gate
                it.fallbackError = IllegalStateException("late fallback failure")
            }
            val manager = h.manager(legacy, directSession = direct, gateway = incoming, outboundGateway = outgoing) { invite() }
            if (inbound) manager.answerTakeover(hosted, "offer-A", direct = true)
            else manager.startCall(hosted, "10086")
            runCurrent()
            direct.emit(SessionEvent.Edge(Signaling.Event.MediaFailed("no_phone_audio")))
            runCurrent()
            assertTrue(if (inbound) "fallback" in incoming.calls else outgoing.calls.any { it.startsWith("fallback:") })
            manager.hangup()
            runCurrent()
            if (!failAfterNextCall) gate.complete(Unit)
            runCurrent()
            assertTrue("inbound=$inbound ${manager.state.value}", manager.isActive)
            assertFalse(direct.disconnected)
            // 旧回落失败不能发布 Failed 给用户开启 B 的机会。
            manager.startCall(pairing, "20000")
            runCurrent()
            assertFalse(legacy.connected)
            advanceTimeBy(1_500)
            runCurrent()
            manager.startCall(pairing, "20000")
            runCurrent()
            assertEquals(CallState.WaitingMedia("20000"), manager.state.value)
            if (failAfterNextCall) {
                gate.complete(Unit)
                runCurrent()
            }
            advanceTimeBy(2_000)
            runCurrent()
            assertEquals(CallState.WaitingMedia("20000"), manager.state.value)
            assertFalse(legacy.disconnected)
            h.close()
        }
    }

    @Test
    fun `旧挂断释放资源期间开启新通不得被旧Ended覆写`() = runTest {
        val h = Harness(this)
        lateinit var manager: CallManager
        val old = object : FakeDirectSession() {
            override fun disconnect() {
                super.disconnect()
                manager.startCall(pairing, "20000")
            }
        }.also { it.autoAckHangup = false }
        val next = FakeSession()
        manager = h.manager(next, directSession = old, outboundGateway = FakeOutboundGateway()) { invite() }
        manager.startCall(hosted, "10086")
        runCurrent()
        manager.hangup()
        advanceTimeBy(1_500)
        runCurrent()
        assertEquals(CallState.WaitingMedia("20000"), manager.state.value)
        assertFalse(next.disconnected)
        h.close()
    }

    @Test
    fun `挂断带编号 收到 Edge 确认才断开 不重发`() = runTest {
        val h = Harness(this)
        val session = FakeSession().also { it.autoAckHangup = false }
        val manager = h.manager(session) { invite() }
        manager.startCall(pairing, "10000")
        advanceUntilIdle()
        session.emit(SessionEvent.Edge(Signaling.Event.Status("connected")))
        runCurrent()

        manager.hangup()
        runCurrent()
        val ids = hangupIds(session)
        assertTrue(manager.hangingUp.value)
        assertEquals(1, ids.size)
        assertFalse(session.disconnected)

        session.emit(SessionEvent.Edge(Signaling.Event.HangupAck("someone-else")))
        runCurrent()
        assertFalse(session.disconnected)
        for (raw in listOf(
            """{"type":"hangup.ack"}""",
            """{"type":"hangup.ack","commandId":null}""",
            """{"type":"hangup.ack","commandId":123}""",
            """{"type":"hangup.ack","commandId":true}""",
            """{"type":"hangup.ack","commandId":[]}""",
            """{"type":"hangup.ack","commandId":{}}""",
        )) {
            Signaling.decodeEvent(raw)?.let { session.emit(SessionEvent.Edge(it)) }
            runCurrent()
            assertFalse(raw, session.disconnected)
        }
        session.emit(SessionEvent.Edge(Signaling.Event.HangupAck(ids.single())))
        runCurrent()
        assertTrue(session.disconnected)
        assertFalse(manager.hangingUp.value)
        assertEquals(CallState.Ended("10000", "local_hangup"), manager.state.value)
        assertEquals(1, hangupIds(session).size)
        h.close()
    }

    @Test
    fun `挂断没收到确认时重发一次 上限内本地收尾`() = runTest {
        val h = Harness(this)
        val session = FakeSession().also { it.autoAckHangup = false }
        val manager = h.manager(session) { invite() }
        manager.startCall(pairing, "10000")
        advanceUntilIdle()

        manager.hangup()
        runCurrent()
        assertEquals(1, hangupIds(session).size)
        advanceTimeBy(401)
        runCurrent()
        val ids = hangupIds(session)
        assertEquals(2, ids.size)
        assertEquals(ids[0], ids[1])
        assertFalse(session.disconnected)

        advanceTimeBy(1_200)
        runCurrent()
        assertTrue(session.disconnected)
        assertEquals(CallState.Ended("10000", "local_hangup"), manager.state.value)
        h.close()
    }
}
