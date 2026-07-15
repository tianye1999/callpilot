package ai.bondings.callpilot.call

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
    private val takeoverFailureVisibleMs: Long = 2_000L,
) {
    private val scope = scope ?: CoroutineScope(SupervisorJob() + Dispatchers.Default)

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
    fun answerTakeover(pairing: StoredPairing, offerId: String) {
        if (isActive) return
        if (pairing.protocol != PairingProtocol.HOSTED) return
        val label = "来电接管"
        synchronized(commandLock) {
            takeoverMediaTimeoutJob?.cancel()
            takeoverMediaTimeoutJob = null
            hangupRequested = false
            pendingDial = null
        }
        _state.value = CallState.Preparing(label)
        setupJob = scope.launch {
            try {
                val claimed = withContext(ioDispatcher) { takeoverClaimProvider(pairing, offerId) }
                val s = sessionFactory()
                session = s
                eventJob = scope.launch(start = CoroutineStart.UNDISPATCHED) { s.events.collect { handleEvent(label, it) } }
                onForeground(true)
                currentCoroutineContext().ensureActive()
                // WaitingMedia 必须在 connect 之前置位：Edge 的 connected 状态事件
                // 可能在 connect()/麦克风发布期间就到达（接管的物理通话已在进行），
                // 若 connect 后再覆写状态会把 handleEvent 已置的 InCall 打回等待态。
                _state.value = CallState.WaitingMedia(label)
                armTakeoverMediaTimeout(s, label)
                s.connect(claimed.livekitUrl, claimed.token)
                setupJob = null
            } catch (e: CancellationException) {
                throw e
            } catch (e: HostedCloudException) {
                setupJob = null
                cleanup()
                _state.value = CallState.Failed(label, e.message, e.errorCode)
            } catch (e: Exception) {
                setupJob = null
                cleanup()
                _state.value = CallState.Failed(label, e.message ?: "接管失败")
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
                if (isActive) {
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

    private fun armTakeoverMediaTimeout(expectedSession: RemoteSession, label: String) {
        val job = synchronized(commandLock) {
            takeoverMediaTimeoutJob?.cancel()
            scope.launch(start = CoroutineStart.LAZY) {
                delay(takeoverMediaTimeoutMs)
                val failed = CallState.Failed(
                    label,
                    "接管媒体建立超时",
                    code = "TAKEOVER_MEDIA_TIMEOUT",
                )
                val expired = synchronized(commandLock) {
                    if (hangupRequested || session !== expectedSession ||
                        _state.value !is CallState.WaitingMedia
                    ) {
                        false
                    } else {
                        takeoverMediaTimeoutJob = null
                        _state.value = failed
                        true
                    }
                }
                if (!expired) return@launch

                cleanup()
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

    private fun cleanup() {
        synchronized(commandLock) {
            setupJob?.cancel()
            setupJob = null
            dialJob?.cancel()
            dialJob = null
            takeoverMediaTimeoutJob?.cancel()
            takeoverMediaTimeoutJob = null
            pendingDial = null
        }
        eventJob?.cancel()
        eventJob = null
        session?.disconnect()
        session = null
        onForeground(false)
    }
}
