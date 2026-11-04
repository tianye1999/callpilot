package ai.bondings.callpilot.call

import ai.bondings.callpilot.media.DirectCapableSession
import ai.bondings.callpilot.media.RemoteSession
import ai.bondings.callpilot.media.SessionEvent
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.GatewayClient
import ai.bondings.callpilot.protocol.HostedCallSession
import ai.bondings.callpilot.protocol.HostedCloudClient
import ai.bondings.callpilot.protocol.HostedCloudException
import ai.bondings.callpilot.protocol.InboundOffer
import ai.bondings.callpilot.protocol.Invite
import ai.bondings.callpilot.protocol.InviteParser
import ai.bondings.callpilot.protocol.PairingProtocol
import ai.bondings.callpilot.protocol.Signaling
import java.util.UUID
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.sync.withLock

/** 接管未成功但不算失败的结束原因（与 iOS 一致：他机已接、机主已拒绝）。 */
object CallEndReasons {
    const val ANSWERED_ELSEWHERE = "answered_elsewhere"
    const val OWNER_DECLINED = "owner_declined"
}

/** 通话 UI 状态（拨号页/通话页据此切换与渲染）。 */
sealed interface CallState {
    data object Idle : CallState
    data class Preparing(val number: String) : CallState
    data class WaitingMedia(val number: String) : CallState
    data class Dialing(val number: String) : CallState
    data class InCall(val number: String) : CallState
    data class Ended(val number: String, val reason: String) : CallState
    data class Failed(val number: String, val reason: String, val code: String? = null) : CallState
}

/**
 * 出站通话状态机（单通互斥，与 Edge 的一 SIM 一通对应）。
 *
 * 流程：按配对来源创建 Tunnel/v1 session → 连房间发麦 → 发 dial 命令 →
 * 收 Edge status/remote_call 事件推进状态 → 任一端挂断/断连收尾（幂等）。
 */
class CallManager(
    private val sessionFactory: () -> RemoteSession,
    private val inviteProvider: suspend (StoredPairing) -> Invite = { pairing ->
        GatewayClient(pairing.gatewayUrl)
            .also { it.credential = pairing.credential }
            .createSession()
    },
    private val hostedSessionProvider: suspend (StoredPairing, String) -> HostedCallSession =
        { pairing, idempotencyKey ->
            val edgeId = pairing.edgeId ?: error("云配对缺少 Edge ID")
            HostedCloudClient(pairing.gatewayUrl)
                .also { it.credential = pairing.credential }
                .createSession(edgeId, idempotencyKey)
        },
    private val takeoverClaimProvider: suspend (StoredPairing, String) -> HostedCallSession =
        { pairing, offerId ->
            HostedCloudClient(pairing.gatewayUrl)
                .also { it.credential = pairing.credential }
                .claimInboundOffer(offerId)
        },
    private val idempotencyKeyProvider: () -> String = { "android-${UUID.randomUUID()}" },
    private val onForeground: (active: Boolean) -> Unit = {},
    scope: CoroutineScope? = null,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
    private val takeoverMediaTimeoutMs: Long = 20_000L,
    private val incomingCallLabel: () -> String = { "Incoming call" },
    /** J1：正常结束后自动回拨号页的可见时长。 */
    private val endedVisibleMs: Long = 3_000L,
    /** J1：失败结果自动回拨号页的可见时长（比正常结束长，保证原因看得清）。 */
    private val failedVisibleMs: Long = 6_000L,
    /** 直连（Fastpath V2）会话工厂；为 null 表示本机不支持直连，一律走 LiveKit。 */
    private val directSessionFactory: (() -> DirectCapableSession)? = null,
    private val mediaGatewayFactory: (StoredPairing) -> InboundMediaGateway = { HostedInboundMediaGateway(it) },
    /** A8：外呼直连的中转接口。 */
    private val outboundGatewayFactory: (StoredPairing) -> OutboundMediaGateway = { HostedOutboundMediaGateway(it) },
    /** 直连整体上限（含 TURN 收集、补 SDP、ICE、回落），与 iOS 的 60 s 一致。 */
    private val directMediaTimeoutMs: Long = 60_000L,
    private val directSetupMs: Long = 20_000L,
    private val clockMs: () -> Long = System::currentTimeMillis,
    private val timingClockMs: () -> Double = { System.nanoTime() / 1_000_000.0 },
    private val outboundDirectProvider: (StoredPairing) -> Boolean = {
        DirectCallCapabilities.cache.cached(it).outboundDirectV1
    },
    private val outboundTimingLog: (String) -> Unit = { android.util.Log.i("CallPilotDirect", it); Unit },
) {
    // 单线程：建连、回落、挂断收尾、事件处理全部串行，没有「检查完再动手」的窗口。
    @OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
    private val scope = scope ?: CoroutineScope(SupervisorJob() + Dispatchers.Default.limitedParallelism(1))

    private val _state = MutableStateFlow<CallState>(CallState.Idle)
    val state: StateFlow<CallState> = _state

    /** J1：结束页倒计时剩余秒数；null 表示无倒计时（空闲/通话中/已取消/立即返回）。 */
    private val _returnCountdown = MutableStateFlow<Int?>(null)
    val returnCountdown: StateFlow<Int?> = _returnCountdown
    private var returnJob: Job? = null

    init {
        // 终止态由这里统一调度自动返回；非终止态立即取消，避免旧结束页把新通话清掉。
        this.scope.launch {
            _state.collect { next ->
                when (next) {
                    is CallState.Ended -> scheduleAutoReturn(next, endedVisibleMs)
                    is CallState.Failed -> scheduleAutoReturn(next, failedVisibleMs)
                    else -> cancelAutoReturn()
                }
            }
        }
    }

    private var session: RemoteSession? = null
    private var eventJob: Job? = null
    private var setupJob: Job? = null
    private var dialJob: Job? = null
    private var takeoverMediaTimeoutJob: Job? = null
    private val _hangingUp = MutableStateFlow(false)
    val hangingUp: StateFlow<Boolean> = _hangingUp
    private var hangupRequested: Boolean
        get() = _hangingUp.value
        set(value) { _hangingUp.value = value }
    private val commandLock = Any()

    /** 等待 Edge media_ready 后才发出的 dial 命令（Edge 会拒绝抢跑的 dial）。 */
    private var pendingDial: String? = null

    /** 本地挂断等待中的 (commandId, 确认)；Edge 回带同一编号的 hangup.ack 时完成。commandLock 保护。 */
    private var hangupAck: Pair<String, CompletableDeferred<Unit>>? = null

    /** 本通客户端里程碑（G3）：经控制通道发给 Edge，写进本通 events.jsonl。 */
    @Volatile private var timing = CallTiming(timingClockMs)

    /** 当前接管的 offer / claim 与媒体网关：挂断时通知中转结束、直连失败时同 claim 回落。 */
    private var takeover: TakeoverContext? = null

    /** 响铃期提前准备的直连：TURN 凭证 + 对等连接 + 本地 SDP，接听时直接用。 */
    private class PreparedDirect(
        val offerId: String,
        val session: DirectCapableSession,
        val gateway: InboundMediaGateway,
        val offer: Deferred<String?>,
        val expiryJob: Job,
        /** 响铃期预备开始收集的时刻（timingClockMs 单调时钟）。 */
        val preconnectStartAtMs: Double,
        /** 预备 SDP 就绪时刻；收集完成前为 null。 */
        val readyAt: AtomicReference<Double?>,
    )

    /** 当前唯一的预备（只为最新响铃的那通准备）；commandLock 保护。 */
    private var prepared: PreparedDirect? = null

    private class TakeoverContext(val offerId: String, val gateway: InboundMediaGateway?) {
        /** 接听时取走的响铃期预备：在成为本通会话之前归这通接管所有，清理时一并释放。 */
        @Volatile var prepared: PreparedDirect? = null
        @Volatile var claimId: String? = null
        /** claim 独立于 setup 存活：挂断取消 setup 后仍要知道 CAS 是否赢了，赢了就得通知中转结束。 */
        @Volatile var claimJob: Deferred<HostedCallSession>? = null
        @Volatile var direct: DirectCapableSession? = null
        @Volatile var fallbackStarted = false
        @Volatile var fallbackJob: Job? = null
        @Volatile var ended = false
    }

    /** A8：一通直连外呼；建呼叫后才有 callId，回落 LiveKit 恰好一次。 */
    private class OutboundContext(val gateway: OutboundMediaGateway, val session: DirectCapableSession) {
        @Volatile var callId: String? = null
        @Volatile var fallbackStarted = false
        @Volatile var fallbackJob: Job? = null
    }

    /** 当前直连外呼（commandLock 保护）；非直连外呼与来电接管时为 null。 */
    private var outbound: OutboundContext? = null

    val isActive: Boolean
        get() = _state.value.let {
            it !is CallState.Idle && it !is CallState.Ended && it !is CallState.Failed
        }

    fun startCall(pairing: StoredPairing, number: String, outboundTiming: OutboundTiming = OutboundTiming()) {
        if (isActive) return
        outboundTiming.mark(OutboundTiming.Mark.START)
        synchronized(commandLock) {
            takeoverMediaTimeoutJob?.cancel()
            takeoverMediaTimeoutJob = null
            hangupRequested = false
        }
        _state.value = CallState.Preparing(number)
        val idempotencyKey = idempotencyKeyProvider()
        timing = CallTiming(timingClockMs).also { it.restartAt(CallTiming.Mark.DIAL_START) }
        setupJob = scope.launch {
            var directAttempted = false
            try {
                val direct = if (pairing.protocol == PairingProtocol.HOSTED && directSessionFactory != null) {
                    try {
                        outboundDirectProvider(pairing)
                    } catch (error: CancellationException) {
                        throw error
                    } catch (_: Exception) {
                        false
                    }
                } else false
                currentCoroutineContext().ensureActive()
                directAttempted = direct
                if (direct &&
                    startOutboundDirect(pairing, number, idempotencyKey, outboundTiming)
                ) {
                    setupJob = null
                    return@launch
                }
                val (livekitUrl, token) = withContext(ioDispatcher) {
                    when (pairing.protocol) {
                        PairingProtocol.TUNNEL -> connectionFromInvite(inviteProvider(pairing))
                        PairingProtocol.HOSTED -> hostedSessionProvider(pairing, idempotencyKey).let {
                            it.livekitUrl to it.token
                        }
                    }
                }
                val s = sessionFactory()
                session = s
                eventJob = scope.launch(start = CoroutineStart.UNDISPATCHED) { s.events.collect { handleEvent(number, it) } }
                onForeground(true)
                // Edge 在确认本端音频轨就绪（media_ready 事件）前会拒绝 dial，
                // 所以 dial 挂起到 handleEvent 收到 media_ready 再发。
                currentCoroutineContext().ensureActive()
                synchronized(commandLock) {
                    if (_state.value !is CallState.Preparing) throw CancellationException()
                    pendingDial = number
                }
                timing.mark(CallTiming.Mark.CONNECT_START)
                s.connect(livekitUrl, token)
                timing.mark(CallTiming.Mark.CONNECT_RETURN)
                currentCoroutineContext().ensureActive()
                _state.value = CallState.WaitingMedia(number)
                flushTiming(s)
                setupJob = null
            } catch (e: CancellationException) {
                throw e
            } catch (e: HostedCloudException) {
                setupJob = null
                cleanup()
                _state.value = CallState.Failed(number, e.message, e.errorCode)
            } catch (e: Exception) {
                setupJob = null
                cleanup()
                _state.value = CallState.Failed(number, e.message ?: "拨号失败")
            } finally {
                if (directAttempted) outboundTimingLog(outboundTiming.line())
            }
        }
    }

    /**
     * A8：外呼直连。取 TURN → 建对等连接收集完整 offer → 建呼叫时把 offer 交给 Edge，answer 随响应返回 → 连通。
     * 建呼叫之前任何一步不可用都返回 false，由调用方照旧走 LiveKit；建呼叫之后的失败都在同一通里回落 LiveKit。
     */
    private suspend fun startOutboundDirect(pairing: StoredPairing, number: String, idempotencyKey: String, outboundTiming: OutboundTiming): Boolean {
        val edgeId = pairing.edgeId ?: return false
        val factory = directSessionFactory ?: return false
        val gateway = outboundGatewayFactory(pairing)
        gateway.observeOutboundTiming(outboundTiming)
        timing.mark(CallTiming.Mark.PREPARE_START)
        outboundTiming.mark(OutboundTiming.Mark.ICE_START)
        val servers = try {
            gateway.iceServers(edgeId).also {
                timing.mark(CallTiming.Mark.PREPARE_RESPONSE)
                outboundTiming.mark(OutboundTiming.Mark.ICE_READY)
            }
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            return false
        }
        val s = factory()
        s.observeOutboundTiming(outboundTiming)
        timing.mark(CallTiming.Mark.PRECONNECT_START)
        val offer = try {
            withTimeout(directSetupMs) { s.prepareDirect(servers) }.also { timing.mark(CallTiming.Mark.PRECONNECT_READY) }
        } catch (e: TimeoutCancellationException) {
            s.disconnect()
            return false
        } catch (e: CancellationException) {
            s.disconnect()
            throw e
        } catch (_: Exception) {
            s.disconnect()
            return false
        }
        val context = OutboundContext(gateway, s)
        synchronized(commandLock) {
            if (hangupRequested || _state.value !is CallState.Preparing) {
                s.disconnect()
                throw CancellationException("call ended before direct setup")
            }
            session = s
            outbound = context
            pendingDial = number
        }
        eventJob = scope.launch(start = CoroutineStart.UNDISPATCHED) { s.events.collect { handleEvent(number, it) } }
        onForeground(true)
        timing.mark(CallTiming.Mark.CLAIM_START)
        val created = gateway.create(edgeId, idempotencyKey, offer)
        timing.mark(CallTiming.Mark.CLAIM_RESPONSE)
        context.callId = created.callId
        currentCoroutineContext().ensureActive()
        _state.value = CallState.WaitingMedia(number)
        armTakeoverMediaTimeout(s, number, directMediaTimeoutMs, "通话媒体建立超时", "CALL_MEDIA_TIMEOUT")
        val answer = created.sdpAnswer
        when {
            answer != null -> try {
                timing.mark(CallTiming.Mark.CONNECT_START)
                withTimeout(directSetupMs) { s.connectDirect(answer) }
                timing.mark(CallTiming.Mark.CONNECT_RETURN)
                timing.mark(CallTiming.Mark.DIRECT_CONTROL_READY)
                flushTiming(s)
            } catch (e: TimeoutCancellationException) {
                // 阶段超时是直连失败，不是整通被取消：同一通回落。
                outboundFallback(context)
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                outboundFallback(context)
            }
            created.directAttempted -> outboundFallback(context)
            else -> {
                // 中转没走直连（未开启或旧 Edge）：这条对等连接用不上，照旧轮询拿 LiveKit 凭证。
                s.closeDirect()
                val legacy = gateway.awaitSession(edgeId, created)
                currentCoroutineContext().ensureActive()
                timing.mark(CallTiming.Mark.CONNECT_START)
                s.connect(legacy.livekitUrl, legacy.token)
                timing.mark(CallTiming.Mark.CONNECT_RETURN)
                flushTiming(s)
            }
        }
        return true
    }

    /** A8：同一通切到 LiveKit，恰好一次；每个异步返回点都核对这通仍是当前外呼且没被挂断。 */
    private suspend fun outboundFallback(context: OutboundContext) {
        val callId = context.callId ?: return
        synchronized(commandLock) {
            // 回落资格只看 dial 是否已交出（pendingDial 在交出时清空），不看远端确认后才变的界面状态。
            if (context.fallbackStarted || hangupRequested || outbound !== context || pendingDial == null ||
                _state.value !is CallState.WaitingMedia
            ) {
                return
            }
            context.fallbackStarted = true
        }
        timing.mark(CallTiming.Mark.COLD_FALLBACK)
        context.session.closeDirect()
        val fallback = context.gateway.fallbackLiveKit(callId)
        currentCoroutineContext().ensureActive()
        val stillCurrent = synchronized(commandLock) {
            outbound === context && session === context.session && !hangupRequested && _state.value is CallState.WaitingMedia
        }
        if (!stillCurrent) return
        // 每个里程碑只记首次（同 iOS）：直连已记过的 connect_start 保留，这里补齐缺的那端。
        timing.mark(CallTiming.Mark.CONNECT_START)
        context.session.connect(fallback.livekitUrl, fallback.token)
        timing.mark(CallTiming.Mark.CONNECT_RETURN)
        timing.mark(CallTiming.Mark.ROOM_CONNECTED)
        flushTiming(context.session)
    }

    private fun outboundFallbackIfPossible(context: OutboundContext, number: String, reason: String) {
        val eligible = synchronized(commandLock) { pendingDial != null }
        if (!eligible || context.fallbackStarted || _state.value !is CallState.WaitingMedia) return
        context.fallbackJob = scope.launch {
            try {
                outboundFallback(context)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                val captured = synchronized(commandLock) {
                    if (outbound !== context || hangupRequested || !isActive) return@launch
                    _state.value = CallState.Failed(number, e.message ?: reason, (e as? HostedCloudException)?.errorCode)
                    captureForCleanupLocked()
                }
                release(captured)
            }
        }
    }

    /**
     * #95 inbound takeover：claim 一个来电 offer 并入房接管（hosted-only）。
     * 与 startCall 的差异：不发 dial（物理通话已在 Edge 侧进行），入房即等
     * Edge commit 切流后经既有 status 事件推进到 InCall。
     */
    /**
     * 响铃时就开始准备直连（与 iOS 在 VoIP 推送到达时预备一致）：取 TURN、建对等连接、收集到 relay 候选。
     * 只建连接不开麦（音频设备要等连通后才开始采集）。接听时 claim 直接带上已就绪的 SDP，
     * Edge 的 answer 随 claim 返回，省掉接听后的收集与补交往返。没接、他机接、拒绝、过期都会丢弃。
     */
    fun prepareTakeover(pairing: StoredPairing, offer: InboundOffer) {
        val factory = directSessionFactory ?: return
        if (pairing.protocol != PairingProtocol.HOSTED || isActive) return
        val stale = synchronized(commandLock) {
            val current = prepared
            if (current?.offerId == offer.offerId) return
            val session = factory()
            val gateway = mediaGatewayFactory(pairing)
            val preconnectStartAt = timingClockMs()
            val readyAt = AtomicReference<Double?>()
            val sdp = scope.async {
                try {
                    val servers = gateway.iceServers(offer.offerId)
                    val sdpOffer = withTimeout(directSetupMs) { session.prepareDirect(servers) }
                    readyAt.set(timingClockMs())
                    sdpOffer
                } catch (_: TimeoutCancellationException) {
                    null // 收集超时等同预备失败：接听时回落，不另起一份
                } catch (e: CancellationException) {
                    throw e
                } catch (_: Exception) {
                    null // 预备失败不影响接听：接听时走现有流程
                }
            }
            val expiry = scope.launch {
                delay((offer.expiresAt - clockMs() + PREPARED_GRACE_MS).coerceAtLeast(0))
                discardPrepared(offer.offerId)
            }
            prepared = PreparedDirect(offer.offerId, session, gateway, sdp, expiry, preconnectStartAt, readyAt)
            current
        }
        stale?.let { release(it) }
    }

    /** 这通没在本机接听（拒绝 / 他机接 / 结束 / 过期）：放掉预备的连接。 */
    fun discardPrepared(offerId: String) {
        val stale = synchronized(commandLock) {
            prepared?.takeIf { it.offerId == offerId }?.also { prepared = null }
        } ?: return
        release(stale)
    }

    private fun release(prepared: PreparedDirect) {
        prepared.expiryJob.cancel()
        prepared.offer.cancel()
        prepared.session.disconnect()
    }

    private fun takePrepared(offerId: String): PreparedDirect? {
        val (match, other) = synchronized(commandLock) {
            val current = prepared
            prepared = null
            if (current?.offerId == offerId) current to null else null to current
        }
        other?.let { release(it) }
        match?.expiryJob?.cancel()
        return match
    }

    /**
     * 接听时有界等待响铃期直连预备的 SDP 就绪，便于随 claim 一次提交。
     * 未开始 / 已失败 / 已取消 / 超时都返回 null，照旧「先 claim、后补交」；等待中的挂断会取消本协程。
     */
    private suspend fun awaitPreparedSdp(ready: PreparedDirect?): String? {
        if (ready == null) return null
        val offer = ready.offer
        if (offer.isCancelled) return null
        val sdp = if (offer.isCompleted) {
            offer.getCompleted()
        } else {
            try {
                withTimeoutOrNull(PRECONNECT_CLAIM_WAIT_MS) { offer.await() }
            } catch (e: CancellationException) {
                // 接听被取消（挂断）要继续抛出；预备本身被取消则当作未就绪。
                currentCoroutineContext().ensureActive()
                if (offer.isCancelled) null else throw e
            }
        }
        if (sdp != null) {
            timing.mark(CallTiming.Mark.PRECONNECT_READY, atMs = ready.readyAt.get() ?: timingClockMs())
        }
        return sdp
    }

    fun answerTakeover(pairing: StoredPairing, offerId: String, direct: Boolean = false) {
        if (isActive) return
        if (pairing.protocol != PairingProtocol.HOSTED) return
        val label = incomingCallLabel()
        synchronized(commandLock) {
            takeoverMediaTimeoutJob?.cancel()
            takeoverMediaTimeoutJob = null
            hangupRequested = false
            pendingDial = null
        }
        _state.value = CallState.Preparing(label)
        timing = CallTiming(timingClockMs).also { it.restartAt(CallTiming.Mark.ANSWER_ACTION) }
        val wantDirect = direct && directSessionFactory != null
        val prep = takePrepared(offerId)
        if (!wantDirect && prep != null) release(prep)
        val ready = if (wantDirect) prep else null
        // 响铃期预备已开始就记下开始时刻（相对 claim 起点为负，与 iOS 对齐）。
        ready?.let { timing.mark(CallTiming.Mark.PRECONNECT_START, atMs = it.preconnectStartAtMs) }
        val context = TakeoverContext(offerId, if (wantDirect) ready?.gateway ?: mediaGatewayFactory(pairing) else null)
        context.prepared = ready
        synchronized(commandLock) { takeover = context }
        setupJob = scope.launch {
            try {
                val gateway = context.gateway
                // 响铃期预备仍在收集中：有界等它拿到 SDP，随 claim 一次提交；超时/失败/未开始都不等。
                val sdpNow = if (wantDirect && gateway != null) awaitPreparedSdp(ready) else null
                timing.restartAt(CallTiming.Mark.CLAIM_START)
                val claimed = if (wantDirect && gateway != null) {
                    // claim 作为 scope 的独立子任务：挂断取消 setup 时它照常跑完，结果交给 hangup 收尾。
                    // 在锁内创建并发布（LAZY），挂断要么看见它、要么阻止它启动，不存在没人收尾的 claim。
                    val claimJob = synchronized(commandLock) {
                        if (hangupRequested) throw CancellationException("hangup before claim")
                        scope.async(start = CoroutineStart.LAZY) { gateway.claim(offerId, direct = true, sdpOffer = sdpNow) }
                            .also { context.claimJob = it }
                    }
                    claimJob.start()
                    claimJob.await()
                } else {
                    withContext(ioDispatcher) { takeoverClaimProvider(pairing, offerId) }
                }
                context.claimId = claimed.sessionId
                timing.mark(CallTiming.Mark.CLAIM_RESPONSE)
                val useDirect = wantDirect && claimed.directAttempted
                if (!useDirect && ready != null) {
                    context.prepared = null
                    release(ready)
                }
                val s: RemoteSession = if (useDirect) ready?.session ?: directSessionFactory!!() else sessionFactory()
                synchronized(commandLock) {
                    session = s
                    // 预备的连接成了本通会话：之后由会话的清理负责，不再单独释放。
                    if (s === ready?.session) context.prepared = null
                }
                eventJob = scope.launch(start = CoroutineStart.UNDISPATCHED) { s.events.collect { handleEvent(label, it) } }
                onForeground(true)
                currentCoroutineContext().ensureActive()
                // WaitingMedia 必须在 connect 之前置位：Edge 的 connected 状态事件
                // 可能在 connect()/麦克风发布期间就到达（接管的物理通话已在进行），
                // 若 connect 后再覆写状态会把 handleEvent 已置的 InCall 打回等待态。
                _state.value = CallState.WaitingMedia(label)
                armTakeoverMediaTimeout(s, label, if (useDirect) directMediaTimeoutMs else takeoverMediaTimeoutMs)
                if (useDirect && s is DirectCapableSession) {
                    context.direct = s
                    runDirect(context, claimed, s, label, ready?.offer)
                } else {
                    timing.mark(CallTiming.Mark.CONNECT_START)
                    s.connect(claimed.livekitUrl, claimed.token)
                    timing.mark(CallTiming.Mark.CONNECT_RETURN)
                    timing.mark(CallTiming.Mark.ROOM_CONNECTED)
                }
                flushTiming(s)
                setupJob = null
            } catch (e: CancellationException) {
                throw e
            } catch (e: HostedCloudException) {
                setupJob = null
                cleanup()
                _state.value = when (e.errorCode) {
                    // 他机已接 / 机主已拒绝不是失败：按结束原因展示。
                    "OFFER_ANSWERED_ELSEWHERE" -> CallState.Ended(label, CallEndReasons.ANSWERED_ELSEWHERE)
                    "OFFER_OWNER_DECLINED" -> CallState.Ended(label, CallEndReasons.OWNER_DECLINED)
                    else -> CallState.Failed(label, e.message, e.errorCode)
                }
            } catch (e: Exception) {
                setupJob = null
                cleanup()
                _state.value = CallState.Failed(label, e.message ?: "接管失败")
            }
        }
    }

    /**
     * 直连：claim 已成功，这里拿 TURN 凭证 → 建对等连接 → 补交 SDP → 用 Edge 的 answer 连通。
     * 任何一步失败（除 401/409 这类归属性错误）都在同一 claim 下回落 LiveKit，不重新 claim。
     */
    private suspend fun runDirect(
        context: TakeoverContext,
        claimed: HostedCallSession,
        s: DirectCapableSession,
        label: String,
        preparedOffer: Deferred<String?>? = null,
    ) {
        val gateway = context.gateway ?: return
        try {
            var answer = claimed.sdpAnswer
            if (answer == null) {
                val offer = if (preparedOffer != null) {
                    // 同一个对等连接的预备还在收集：等它（同一阶段上限），不另起一份。
                    withTimeout(directSetupMs) { preparedOffer.await() }
                        ?: throw IllegalStateException("响铃期直连预备失败")
                } else {
                    val servers = claimed.iceServers.ifEmpty { gateway.iceServers(context.offerId) }
                    withTimeout(directSetupMs) { s.prepareDirect(servers) }
                }
                currentCoroutineContext().ensureActive()
                timing.mark(CallTiming.Mark.SDP_SUBMIT_START)
                answer = gateway.completeSdp(context.offerId, claimed.sessionId, offer).sdpAnswer
                timing.mark(CallTiming.Mark.SDP_SUBMIT_RESPONSE)
            }
            val sdp = answer ?: throw IllegalStateException("Edge 未给出直连 answer")
            timing.mark(CallTiming.Mark.CONNECT_START)
            withTimeout(directSetupMs) { s.connectDirect(sdp) }
            timing.mark(CallTiming.Mark.CONNECT_RETURN)
            timing.mark(CallTiming.Mark.DIRECT_CONTROL_READY)
        } catch (e: TimeoutCancellationException) {
            // 阶段超时是直连失败，不是整通被取消：同 claim 回落。
            startFallback(context, s, label)
        } catch (e: CancellationException) {
            throw e
        } catch (e: HostedCloudException) {
            if (e.statusCode == 401 || e.statusCode == 409) throw e
            startFallback(context, s, label)
        } catch (e: Exception) {
            startFallback(context, s, label)
        }
    }

    /**
     * 同 claim 回落 LiveKit，恰好一次。先关直连（旧对等连接的断开事件从此不再发出），再向中转换房间凭证；
     * 每个异步返回点都核对这通仍是当前接管且没被挂断，否则丢弃结果而不碰新通话。
     */
    private suspend fun startFallback(context: TakeoverContext, s: DirectCapableSession, label: String) {
        val claimId = context.claimId ?: return
        val gateway = context.gateway ?: return
        synchronized(commandLock) {
            if (context.fallbackStarted || hangupRequested || takeover !== context || _state.value !is CallState.WaitingMedia) return
            context.fallbackStarted = true
        }
        timing.mark(CallTiming.Mark.COLD_FALLBACK)
        s.closeDirect()
        val fallback = gateway.fallbackLiveKit(context.offerId, claimId, timing.httpBatches())
        currentCoroutineContext().ensureActive()
        val stillCurrent = synchronized(commandLock) {
            takeover === context && session === s && !hangupRequested && _state.value is CallState.WaitingMedia
        }
        if (!stillCurrent) return
        timing.mark(CallTiming.Mark.CONNECT_START)
        s.connect(fallback.livekitUrl, fallback.token)
        timing.mark(CallTiming.Mark.CONNECT_RETURN)
        timing.mark(CallTiming.Mark.ROOM_CONNECTED)
        flushTiming(s)
    }

    private fun fallbackIfPossible(reason: String) {
        val context = takeover ?: return
        val direct = context.direct ?: return
        val label = currentNumber() ?: return
        if (hangupRequested || context.fallbackStarted || _state.value !is CallState.WaitingMedia) return
        val job = scope.launch {
            try {
                startFallback(context, direct, label)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                val captured = synchronized(commandLock) {
                    if (takeover !== context || hangupRequested || !isActive) return@launch
                    _state.value = CallState.Failed(label, e.message ?: reason, (e as? HostedCloudException)?.errorCode)
                    captureForCleanupLocked()
                }
                release(captured)
            }
        }
        context.fallbackJob = job
    }

    /** claim 可能在挂断后才返回：赢了就通知中转结束，不留下无人收尾的接管。 */
    private fun endClaimAfterHangup(context: TakeoverContext) {
        val gateway = context.gateway ?: return
        val callTiming = timing
        val claimJob = synchronized(commandLock) {
            // 还没发出 claim（LAZY 任务未创建）就不会再发：answerTakeover 在同一把锁下看到 hangupRequested 会放弃。
            if (context.ended) return
            val job = context.claimJob
            if (job == null && context.claimId == null) return
            context.ended = true
            job
        }
        scope.launch {
            val claimId = context.claimId ?: try {
                claimJob?.await()?.sessionId
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                null
            } ?: return@launch
            try {
                gateway.end(context.offerId, claimId, callTiming.httpBatches())
            } catch (_: Exception) {
                // 中转结束通知是尽力而为；数据通道的 hangup 与 Edge 的收尾不依赖它。
            }
        }
    }

    fun sendDtmf(digits: String) {
        val s = session ?: return
        scope.launch {
            try {
                s.sendCommand(Signaling.encodeDtmf(digits))
            } catch (_: Exception) {
                // DTMF 失败不改变通话状态
            }
        }
    }

    fun setSpeakerphone(enabled: Boolean) {
        session?.setSpeakerphone(enabled)
    }

    fun hangup() {
        val deadline = timingClockMs() + HANGUP_ACK_BUDGET_MS
        val commandId = UUID.randomUUID().toString()
        val acknowledged = CompletableDeferred<Unit>()
        val number: String
        val s: RemoteSession?
        val callTiming: CallTiming
        synchronized(commandLock) {
            number = currentNumber() ?: return
            if (hangupRequested) return
            hangupRequested = true
            s = session
            callTiming = timing.also { it.mark(CallTiming.Mark.HANGUP_INTENT) }
            hangupAck = commandId to acknowledged
            pendingDial = null
            setupJob?.cancel()
            setupJob = null
            dialJob?.cancel()
            dialJob = null
            takeover?.fallbackJob?.cancel()
            outbound?.fallbackJob?.cancel()
            takeover?.let { endClaimAfterHangup(it) }
        }
        scope.launch {
            if (s != null) {
                // 等 Edge 回 hangup.ack 再断开（同 iOS）：发完立刻断开房间时，LiveKit 的数据包常常还没发出去，
                // Edge 只能等媒体断开再挂（11-03 真机）。没回就重发一次；总时长有上限，命令发不出去也照样本地收尾。
                // 点击时即确定单调时钟 deadline：调度延迟、遥测、发送和确认共用预算。
                withTimeoutOrNull((deadline - timingClockMs()).toLong().coerceAtLeast(0L)) {
                    withTimeoutOrNull(HANGUP_TIMING_BUDGET_MS) { sendTiming(s, callTiming) }
                    for (attempt in 0 until 2) {
                        try {
                            s.sendCommand(Signaling.encodeHangup(commandId))
                        } catch (e: CancellationException) {
                            throw e
                        } catch (_: Exception) {
                            // 下一次重发或超时后本地收尾；Edge 有断线 grace 兜底挂断。
                        }
                        if (withTimeoutOrNull(HANGUP_RETRY_MS) { acknowledged.await() } != null) return@withTimeoutOrNull
                    }
                    acknowledged.await()
                }
                if (acknowledged.isCompleted) callTiming.mark(CallTiming.Mark.HANGUP_ACK)
            }
            val captured = synchronized(commandLock) {
                // 每通都有独立 timing，既作 generation 也覆盖尚未创建 session 的挂断。
                if (timing !== callTiming || hangupAck?.second !== acknowledged) return@launch
                hangupAck = null
                hangupRequested = false
                _state.value = CallState.Ended(number, "local_hangup")
                captureForCleanupLocked()
            }
            release(captured)
        }
    }

    /** UI 从 Ended/Failed 回到拨号页。 */
    fun reset() {
        if (!isActive) {
            cancelAutoReturn()
            _state.value = CallState.Idle
        }
    }

    /** J1：用户点按/滚动结束页时取消自动返回，把返回时机交还用户。 */
    fun cancelAutoReturn() {
        returnJob?.cancel()
        returnJob = null
        _returnCountdown.value = null
    }

    /** J1：终止结果倒计时；仅当仍是同一通结果时才回到 Idle。 */
    private fun scheduleAutoReturn(terminal: CallState, visibleMs: Long) {
        returnJob?.cancel()
        var remaining = ((visibleMs + 999) / 1_000).toInt().coerceAtLeast(1)
        _returnCountdown.value = remaining
        returnJob = scope.launch {
            while (remaining > 0) {
                delay(1_000)
                if (_state.value !== terminal) return@launch
                remaining -= 1
                if (remaining == 0) {
                    _returnCountdown.value = null
                    _state.value = CallState.Idle
                } else {
                    _returnCountdown.value = remaining
                }
            }
        }
    }

    private fun handleEvent(number: String, event: SessionEvent) {
        val ack = (event as? SessionEvent.Edge)?.event as? Signaling.Event.HangupAck
        if (ack != null) {
            // 挂断确认要在「已请求挂断」的过滤之前处理：它正是挂断之后才到的事件。
            val pending = synchronized(commandLock) { hangupAck }
            if (pending != null && ack.commandId == pending.first) pending.second.complete(Unit)
            return
        }
        if (!isActive || synchronized(commandLock) { hangupRequested }) return
        when (event) {
            is SessionEvent.Edge -> {
                // Edge 经 LiveKit data channel 发的是 type=status 事件（#37 契约）；
                // type=remote_call 只存在于 Edge 本地面板，这里兼容消费以防协议演进。
                val (status, reason) = when (val e = event.event) {
                    is Signaling.Event.Status -> e.status to e.reason
                    is Signaling.Event.RemoteCall -> e.status to null
                    is Signaling.Event.Ended -> {
                        cleanup()
                        _state.value = CallState.Ended(number, e.reason)
                        return
                    }
                    is Signaling.Event.MediaFailed -> {
                        // 直连通了但 Edge 收不到手机音频：提交前回落 LiveKit，提交后由 Edge 处置。
                        val out = outbound
                        if (out != null) outboundFallbackIfPossible(out, number, e.reason) else fallbackIfPossible(e.reason)
                        return
                    }
                    is Signaling.Event.HangupAck -> return
                }
                when (status) {
                    "media_ready" -> {
                        session?.let { flushTiming(it) }
                        firePendingDial()
                    }
                    "dialing" -> _state.value = CallState.Dialing(number)
                    "connected" -> markConnected(number)
                    "ended", "hangup" -> {
                        cleanup()
                        _state.value = CallState.Ended(number, reason ?: status)
                    }
                    "failed" -> {
                        cleanup()
                        _state.value = CallState.Failed(
                            number, reason ?: status, (event.event as? Signaling.Event.Status)?.code,
                        )
                    }
                    // waiting_for_phone 等生命周期提示不改变状态
                    else -> Unit
                }
            }
            is SessionEvent.Disconnected -> {
                val out = outbound
                if (out != null && (event.reason.startsWith("direct_") || event.reason == "channel_closed")) {
                    // 回落进行中的旧对等连接尾随事件与本通无关；dial 交出前直连断开则同一通回落，交出后按结束处理。
                    if (out.fallbackStarted) return
                    val dialPending = synchronized(commandLock) { pendingDial != null }
                    if (dialPending && _state.value is CallState.WaitingMedia) {
                        outboundFallbackIfPossible(out, number, event.reason)
                        return
                    }
                }
                val context = takeover
                val direct = context?.direct
                val fromDirect = event.reason.startsWith("direct_") || event.reason == "channel_closed"
                if (direct != null && context.fallbackStarted && fromDirect) {
                    // 回落进行中，旧对等连接的尾随断开事件与本通无关。
                    return
                }
                if (direct != null && !context.fallbackStarted && _state.value is CallState.WaitingMedia) {
                    // 直连在提交前断开：同 claim 回落，而不是把通话当成结束。
                    fallbackIfPossible(event.reason)
                } else if (isActive) {
                    cleanup()
                    _state.value = CallState.Ended(number, event.reason)
                }
            }
        }
    }

    /** media_ready 后才把 dial 发给 Edge（幂等：只发一次）。 */
    private fun firePendingDial() {
        val job = synchronized(commandLock) {
            if (hangupRequested ||
                (_state.value !is CallState.WaitingMedia && _state.value !is CallState.Dialing)
            ) {
                return
            }
            val number = pendingDial ?: return
            pendingDial = null
            val s = session ?: return
            scope.launch(start = CoroutineStart.LAZY) {
                if (!dialIsActive()) return@launch
                try {
                    s.sendCommand(Signaling.encodeDial(number, UUID.randomUUID().toString()))
                    if (!dialIsActive()) return@launch
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    synchronized(commandLock) { dialJob = null }
                    cleanup()
                    _state.value = CallState.Failed(number, e.message ?: "拨号失败")
                }
            }.also { dialJob = it }
        }
        job.start()
    }

    private fun dialIsActive(): Boolean = synchronized(commandLock) {
        val state = _state.value
        !hangupRequested &&
            (state is CallState.WaitingMedia || state is CallState.Dialing) &&
            dialJob?.isActive == true
    }

    private fun armTakeoverMediaTimeout(
        expectedSession: RemoteSession,
        label: String,
        timeoutMs: Long = takeoverMediaTimeoutMs,
        message: String = "接管媒体建立超时",
        code: String = "TAKEOVER_MEDIA_TIMEOUT",
    ) {
        val job = synchronized(commandLock) {
            takeoverMediaTimeoutJob?.cancel()
            scope.launch(start = CoroutineStart.LAZY) {
                delay(timeoutMs)
                val failed = CallState.Failed(label, message, code = code)
                // 校验所属会话、发布 Failed、摘取这通的资源是同一次锁内转换：
                // 看到 Failed 后立刻开始的下一通，不可能被这里的清理碰到。
                val captured = synchronized(commandLock) {
                    if (hangupRequested || session !== expectedSession ||
                        _state.value !is CallState.WaitingMedia
                    ) {
                        null
                    } else {
                        takeoverMediaTimeoutJob = null
                        _state.value = failed
                        captureForCleanupLocked()
                    }
                } ?: return@launch

                release(captured)
                // Failed 的自动回 Idle 由 state 收集器按 failedVisibleMs 统一调度。
            }.also { takeoverMediaTimeoutJob = it }
        }
        job.start()
    }

    private fun markConnected(number: String) {
        val s = synchronized(commandLock) {
            if (hangupRequested || !isActive) return
            takeoverMediaTimeoutJob?.cancel()
            takeoverMediaTimeoutJob = null
            _state.value = CallState.InCall(number)
            session
        }
        timing.mark(CallTiming.Mark.EDGE_CONNECTED)
        s?.let { flushTiming(it) }
    }

    /** 把还没发出的里程碑经控制通道补给 Edge（尽力而为；失败的下次重发，不影响通话）。 */
    private fun flushTiming(s: RemoteSession) {
        // 发起时就绑定本通的计时：协程晚跑时新一通可能已换了 timing，不能把新通的里程碑发到旧会话。
        val callTiming = timing
        scope.launch { sendTiming(s, callTiming) }
    }

    private suspend fun sendTiming(s: RemoteSession, callTiming: CallTiming) {
        callTiming.sendLock.withLock {
            while (true) {
                val batch = callTiming.nextBatch() ?: return
                try {
                    s.sendCommand(batch.text)
                } catch (e: CancellationException) {
                    callTiming.unsend(batch)
                    throw e
                } catch (_: Exception) {
                    callTiming.unsend(batch)
                    return
                }
            }
        }
    }

    private fun currentNumber(): String? = when (val s = _state.value) {
        is CallState.Preparing -> s.number
        is CallState.WaitingMedia -> s.number
        is CallState.Dialing -> s.number
        is CallState.InCall -> s.number
        else -> null
    }

    private fun connectionFromInvite(invite: Invite): Pair<String, String> {
        val structuredUrl = invite.livekitUrl
        val structuredToken = invite.token
        if (!structuredUrl.isNullOrBlank() && !structuredToken.isNullOrBlank()) {
            return structuredUrl to structuredToken
        }
        val payload = InviteParser.parseInviteUrl(invite.url)
            ?: error("邀请解析失败（协议版本或格式不符）")
        return payload.url to payload.token
    }

    /**
     * 只清理此刻捕获到的会话与接管上下文：先在锁内摘下引用并通知前台服务停止，再在锁外断开旧会话。
     * 旧会话 disconnect 可能阻塞在 native 释放上，期间用户已能从界面开始下一通；那一通设置的新
     * session / takeover 不会再被这里清掉。
     */
    private fun cleanup() {
        release(synchronized(commandLock) { captureForCleanupLocked() })
    }

    private class CapturedCall(
        val session: RemoteSession?,
        val takeover: TakeoverContext?,
        val eventJob: Job?,
        val outbound: OutboundContext? = null,
    )

    /** 必须持有 commandLock：摘下当前通话的全部引用并置空，之后的新通话从空状态开始。 */
    private fun captureForCleanupLocked(): CapturedCall {
        setupJob?.cancel()
        setupJob = null
        dialJob?.cancel()
        dialJob = null
        takeoverMediaTimeoutJob?.cancel()
        takeoverMediaTimeoutJob = null
        pendingDial = null
        val captured = CapturedCall(session, takeover, eventJob, outbound)
        session = null
        takeover = null
        outbound = null
        eventJob = null
        return captured
    }

    /** 锁外释放已摘下的旧资源：disconnect 可能阻塞在 native 释放上，不能挡住新通话的状态转换。 */
    private fun release(captured: CapturedCall) {
        onForeground(false)
        captured.eventJob?.cancel()
        captured.takeover?.fallbackJob?.cancel()
        captured.outbound?.fallbackJob?.cancel()
        // claim 失败或 claim 期间挂断：预备还没交给会话，这里放掉。
        captured.takeover?.prepared?.let {
            captured.takeover.prepared = null
            release(it)
        }
        captured.session?.disconnect()
    }
}

/** 挂断等 Edge 确认的总上限与重发间隔（iOS 为 2.5 s 预算、最多两次发送）。 */
private const val HANGUP_ACK_BUDGET_MS = 1_500L
private const val HANGUP_RETRY_MS = 400L

/** 挂断前补发里程碑的上限：只是诊断数据，不能推迟挂断命令。 */
private const val HANGUP_TIMING_BUDGET_MS = 300L

/** 预备在来电过期后再保留一小会儿：服务端时钟与本机略有偏差。 */
private const val PREPARED_GRACE_MS = 2_000L

/** 接听时有界等待响铃期直连 SDP 就绪的上限。取值见任务说明：≤1.2 s。 */
private const val PRECONNECT_CLAIM_WAIT_MS = 1_200L
