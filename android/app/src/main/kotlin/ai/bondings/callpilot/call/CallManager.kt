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
import kotlinx.coroutines.CancellationException
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
    private val takeoverFailureVisibleMs: Long = 2_000L,
    /** 直连（Fastpath V2）会话工厂；为 null 表示本机不支持直连，一律走 LiveKit。 */
    private val directSessionFactory: (() -> DirectCapableSession)? = null,
    private val mediaGatewayFactory: (StoredPairing) -> InboundMediaGateway = { HostedInboundMediaGateway(it) },
    /** A8：外呼直连的中转接口。 */
    private val outboundGatewayFactory: (StoredPairing) -> OutboundMediaGateway = { HostedOutboundMediaGateway(it) },
    /** 直连整体上限（含 TURN 收集、补 SDP、ICE、回落），与 iOS 的 60 s 一致。 */
    private val directMediaTimeoutMs: Long = 60_000L,
    private val directSetupMs: Long = 20_000L,
    private val clockMs: () -> Long = System::currentTimeMillis,
) {
    // 单线程：建连、回落、挂断收尾、事件处理全部串行，没有「检查完再动手」的窗口。
    @OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
    private val scope = scope ?: CoroutineScope(SupervisorJob() + Dispatchers.Default.limitedParallelism(1))

    private val _state = MutableStateFlow<CallState>(CallState.Idle)
    val state: StateFlow<CallState> = _state

    private var session: RemoteSession? = null
    private var eventJob: Job? = null
    private var setupJob: Job? = null
    private var dialJob: Job? = null
    private var takeoverMediaTimeoutJob: Job? = null
    private var hangupRequested = false
    private val commandLock = Any()

    /** 等待 Edge media_ready 后才发出的 dial 命令（Edge 会拒绝抢跑的 dial）。 */
    private var pendingDial: String? = null

    /** 当前接管的 offer / claim 与媒体网关：挂断时通知中转结束、直连失败时同 claim 回落。 */
    private var takeover: TakeoverContext? = null

    /** 响铃期提前准备的直连：TURN 凭证 + 对等连接 + 本地 SDP，接听时直接用。 */
    private class PreparedDirect(
        val offerId: String,
        val session: DirectCapableSession,
        val gateway: InboundMediaGateway,
        val offer: Deferred<String?>,
        val expiryJob: Job,
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

    fun startCall(pairing: StoredPairing, number: String, direct: Boolean = false) {
        if (isActive) return
        synchronized(commandLock) {
            takeoverMediaTimeoutJob?.cancel()
            takeoverMediaTimeoutJob = null
            hangupRequested = false
        }
        _state.value = CallState.Preparing(number)
        val idempotencyKey = idempotencyKeyProvider()
        setupJob = scope.launch {
            try {
                if (direct && pairing.protocol == PairingProtocol.HOSTED &&
                    startOutboundDirect(pairing, number, idempotencyKey)
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
                s.connect(livekitUrl, token)
                currentCoroutineContext().ensureActive()
                _state.value = CallState.WaitingMedia(number)
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
            }
        }
    }

    /**
     * A8：外呼直连。取 TURN → 建对等连接收集完整 offer → 建呼叫时把 offer 交给 Edge，answer 随响应返回 → 连通。
     * 建呼叫之前任何一步不可用都返回 false，由调用方照旧走 LiveKit；建呼叫之后的失败都在同一通里回落 LiveKit。
     */
    private suspend fun startOutboundDirect(pairing: StoredPairing, number: String, idempotencyKey: String): Boolean {
        val edgeId = pairing.edgeId ?: return false
        val factory = directSessionFactory ?: return false
        val gateway = outboundGatewayFactory(pairing)
        val servers = try {
            gateway.iceServers(edgeId)
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            return false
        }
        val s = factory()
        val offer = try {
            withTimeout(directSetupMs) { s.prepareDirect(servers) }
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
        val created = gateway.create(edgeId, idempotencyKey, offer)
        context.callId = created.callId
        currentCoroutineContext().ensureActive()
        _state.value = CallState.WaitingMedia(number)
        armTakeoverMediaTimeout(s, number, directMediaTimeoutMs, "通话媒体建立超时", "CALL_MEDIA_TIMEOUT")
        val answer = created.sdpAnswer
        when {
            answer != null -> try {
                withTimeout(directSetupMs) { s.connectDirect(answer) }
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
                s.connect(legacy.livekitUrl, legacy.token)
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
        context.session.closeDirect()
        val fallback = context.gateway.fallbackLiveKit(callId)
        currentCoroutineContext().ensureActive()
        val stillCurrent = synchronized(commandLock) {
            outbound === context && session === context.session && !hangupRequested && _state.value is CallState.WaitingMedia
        }
        if (!stillCurrent) return
        context.session.connect(fallback.livekitUrl, fallback.token)
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
                val mine = synchronized(commandLock) { outbound === context && isActive }
                if (mine) {
                    cleanup()
                    _state.value = CallState.Failed(number, e.message ?: reason, (e as? HostedCloudException)?.errorCode)
                }
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
            val sdp = scope.async {
                try {
                    val servers = gateway.iceServers(offer.offerId)
                    withTimeout(directSetupMs) { session.prepareDirect(servers) }
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
            prepared = PreparedDirect(offer.offerId, session, gateway, sdp, expiry)
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
        val wantDirect = direct && directSessionFactory != null
        val prep = takePrepared(offerId)
        if (!wantDirect && prep != null) release(prep)
        val ready = if (wantDirect) prep else null
        val context = TakeoverContext(offerId, if (wantDirect) ready?.gateway ?: mediaGatewayFactory(pairing) else null)
        context.prepared = ready
        synchronized(commandLock) { takeover = context }
        setupJob = scope.launch {
            try {
                // 先 claim（CAS 定归属），直连准备在 claim 之后与 Edge 并行进行。
                val gateway = context.gateway
                val claimed = if (wantDirect && gateway != null) {
                    // claim 作为 scope 的独立子任务：挂断取消 setup 时它照常跑完，结果交给 hangup 收尾。
                    // 在锁内创建并发布（LAZY），挂断要么看见它、要么阻止它启动，不存在没人收尾的 claim。
                    val claimJob = synchronized(commandLock) {
                        if (hangupRequested) throw CancellationException("hangup before claim")
                        // 预备的 SDP 已就绪就随 claim 一起发；没就绪也不等，claim 先定归属，之后补交。
                        val sdpNow = ready?.offer?.takeIf { it.isCompleted && !it.isCancelled }?.getCompleted()
                        scope.async(start = CoroutineStart.LAZY) { gateway.claim(offerId, direct = true, sdpOffer = sdpNow) }
                            .also { context.claimJob = it }
                    }
                    claimJob.start()
                    claimJob.await()
                } else {
                    withContext(ioDispatcher) { takeoverClaimProvider(pairing, offerId) }
                }
                context.claimId = claimed.sessionId
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
                    s.connect(claimed.livekitUrl, claimed.token)
                }
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
                answer = gateway.completeSdp(context.offerId, claimed.sessionId, offer).sdpAnswer
            }
            val sdp = answer ?: throw IllegalStateException("Edge 未给出直连 answer")
            withTimeout(directSetupMs) { s.connectDirect(sdp) }
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
        s.closeDirect()
        val fallback = gateway.fallbackLiveKit(context.offerId, claimId)
        currentCoroutineContext().ensureActive()
        val stillCurrent = synchronized(commandLock) {
            takeover === context && session === s && !hangupRequested && _state.value is CallState.WaitingMedia
        }
        if (!stillCurrent) return
        s.connect(fallback.livekitUrl, fallback.token)
    }

    private fun fallbackIfPossible(reason: String) {
        val context = takeover ?: return
        val direct = context.direct ?: return
        val label = currentNumber() ?: return
        if (context.fallbackStarted || _state.value !is CallState.WaitingMedia) return
        val job = scope.launch {
            try {
                startFallback(context, direct, label)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                val mine = synchronized(commandLock) { takeover === context && isActive }
                if (mine) {
                    cleanup()
                    _state.value = CallState.Failed(label, e.message ?: reason, (e as? HostedCloudException)?.errorCode)
                }
            }
        }
        context.fallbackJob = job
    }

    /** claim 可能在挂断后才返回：赢了就通知中转结束，不留下无人收尾的接管。 */
    private fun endClaimAfterHangup(context: TakeoverContext) {
        val gateway = context.gateway ?: return
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
                gateway.end(context.offerId, claimId)
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
        val number = currentNumber() ?: return
        synchronized(commandLock) {
            if (hangupRequested) return
            hangupRequested = true
            pendingDial = null
            setupJob?.cancel()
            setupJob = null
            dialJob?.cancel()
            dialJob = null
        }
        val s = session
        takeover?.let { endClaimAfterHangup(it) }
        scope.launch {
            try {
                s?.sendCommand(Signaling.encodeHangup())
            } catch (_: Exception) {
                // 命令发不出去也要本地收尾；Edge 有断线 grace 兜底挂断
            }
            cleanup()
            _state.value = CallState.Ended(number, "local_hangup")
        }
    }

    /** UI 从 Ended/Failed 回到拨号页。 */
    fun reset() {
        if (!isActive) _state.value = CallState.Idle
    }

    private fun handleEvent(number: String, event: SessionEvent) {
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
                    Signaling.Event.HangupAck -> return
                }
                when (status) {
                    "media_ready" -> firePendingDial()
                    "dialing" -> _state.value = CallState.Dialing(number)
                    "connected" -> markConnected(number)
                    "ended", "hangup" -> {
                        cleanup()
                        _state.value = CallState.Ended(number, reason ?: status)
                    }
                    "failed" -> {
                        cleanup()
                        _state.value = CallState.Failed(number, reason ?: status)
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
                delay(takeoverFailureVisibleMs)
                if (_state.value == failed) _state.value = CallState.Idle
            }.also { takeoverMediaTimeoutJob = it }
        }
        job.start()
    }

    private fun markConnected(number: String) {
        synchronized(commandLock) {
            if (hangupRequested || !isActive) return
            takeoverMediaTimeoutJob?.cancel()
            takeoverMediaTimeoutJob = null
            _state.value = CallState.InCall(number)
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

/** 预备在来电过期后再保留一小会儿：服务端时钟与本机略有偏差。 */
private const val PREPARED_GRACE_MS = 2_000L
