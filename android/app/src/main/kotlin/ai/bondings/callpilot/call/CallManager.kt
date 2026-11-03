package ai.bondings.callpilot.call

import ai.bondings.callpilot.media.DirectCapableSession
import ai.bondings.callpilot.media.RemoteSession
import ai.bondings.callpilot.media.SessionEvent
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.GatewayClient
import ai.bondings.callpilot.protocol.HostedCallSession
import ai.bondings.callpilot.protocol.HostedCloudClient
import ai.bondings.callpilot.protocol.HostedCloudException
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
    /** 直连整体上限（含 TURN 收集、补 SDP、ICE、回落），与 iOS 的 60 s 一致。 */
    private val directMediaTimeoutMs: Long = 60_000L,
    private val directSetupMs: Long = 20_000L,
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

    private class TakeoverContext(val offerId: String, val gateway: InboundMediaGateway?) {
        @Volatile var claimId: String? = null
        /** claim 独立于 setup 存活：挂断取消 setup 后仍要知道 CAS 是否赢了，赢了就得通知中转结束。 */
        @Volatile var claimJob: Deferred<HostedCallSession>? = null
        @Volatile var direct: DirectCapableSession? = null
        @Volatile var fallbackStarted = false
        @Volatile var fallbackJob: Job? = null
        @Volatile var ended = false
    }

    val isActive: Boolean
        get() = _state.value.let {
            it !is CallState.Idle && it !is CallState.Ended && it !is CallState.Failed
        }

    fun startCall(pairing: StoredPairing, number: String) {
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
     * #95 inbound takeover：claim 一个来电 offer 并入房接管（hosted-only）。
     * 与 startCall 的差异：不发 dial（物理通话已在 Edge 侧进行），入房即等
     * Edge commit 切流后经既有 status 事件推进到 InCall。
     */
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
        val context = TakeoverContext(offerId, if (wantDirect) mediaGatewayFactory(pairing) else null)
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
                        scope.async(start = CoroutineStart.LAZY) { gateway.claim(offerId, direct = true) }
                            .also { context.claimJob = it }
                    }
                    claimJob.start()
                    claimJob.await()
                } else {
                    withContext(ioDispatcher) { takeoverClaimProvider(pairing, offerId) }
                }
                context.claimId = claimed.sessionId
                val useDirect = wantDirect && claimed.directAttempted
                val s: RemoteSession = if (useDirect) directSessionFactory!!() else sessionFactory()
                session = s
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
                    runDirect(context, claimed, s, label)
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
    private suspend fun runDirect(context: TakeoverContext, claimed: HostedCallSession, s: DirectCapableSession, label: String) {
        val gateway = context.gateway ?: return
        try {
            var answer = claimed.sdpAnswer
            if (answer == null) {
                val servers = claimed.iceServers.ifEmpty { gateway.iceServers(context.offerId) }
                val offer = withTimeout(directSetupMs) { s.prepareDirect(servers) }
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
                        fallbackIfPossible(e.reason)
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

    private fun armTakeoverMediaTimeout(expectedSession: RemoteSession, label: String, timeoutMs: Long = takeoverMediaTimeoutMs) {
        val job = synchronized(commandLock) {
            takeoverMediaTimeoutJob?.cancel()
            scope.launch(start = CoroutineStart.LAZY) {
                delay(timeoutMs)
                val failed = CallState.Failed(
                    label,
                    "接管媒体建立超时",
                    code = "TAKEOVER_MEDIA_TIMEOUT",
                )
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

    private class CapturedCall(val session: RemoteSession?, val takeover: TakeoverContext?, val eventJob: Job?)

    /** 必须持有 commandLock：摘下当前通话的全部引用并置空，之后的新通话从空状态开始。 */
    private fun captureForCleanupLocked(): CapturedCall {
        setupJob?.cancel()
        setupJob = null
        dialJob?.cancel()
        dialJob = null
        takeoverMediaTimeoutJob?.cancel()
        takeoverMediaTimeoutJob = null
        pendingDial = null
        val captured = CapturedCall(session, takeover, eventJob)
        session = null
        takeover = null
        eventJob = null
        return captured
    }

    /** 锁外释放已摘下的旧资源：disconnect 可能阻塞在 native 释放上，不能挡住新通话的状态转换。 */
    private fun release(captured: CapturedCall) {
        onForeground(false)
        captured.eventJob?.cancel()
        captured.takeover?.fallbackJob?.cancel()
        captured.session?.disconnect()
    }
}
