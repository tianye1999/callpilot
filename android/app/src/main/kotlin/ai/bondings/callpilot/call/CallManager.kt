package ai.bondings.callpilot.call

import ai.bondings.callpilot.media.RemoteSession
import ai.bondings.callpilot.media.SessionEvent
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.GatewayClient
import ai.bondings.callpilot.protocol.Invite
import ai.bondings.callpilot.protocol.InviteParser
import ai.bondings.callpilot.protocol.Signaling
import java.util.UUID
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
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
    data class Failed(val number: String, val reason: String) : CallState
}

/**
 * 出站通话状态机（单通互斥，与 Edge 的一 SIM 一通对应）。
 *
 * 流程：createSession → 解 fragment → 连房间发麦 → 发 dial 命令 →
 * 收 Edge status/remote_call 事件推进状态 → 任一端挂断/断连收尾（幂等）。
 */
class CallManager(
    private val sessionFactory: () -> RemoteSession,
    private val inviteProvider: (StoredPairing) -> Invite = { pairing ->
        GatewayClient(pairing.gatewayUrl)
            .also { it.credential = pairing.credential }
            .createSession()
    },
    private val onForeground: (active: Boolean) -> Unit = {},
    scope: CoroutineScope? = null,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
) {
    private val scope = scope ?: CoroutineScope(SupervisorJob() + Dispatchers.Default)

    private val _state = MutableStateFlow<CallState>(CallState.Idle)
    val state: StateFlow<CallState> = _state

    private var session: RemoteSession? = null
    private var eventJob: Job? = null

    val isActive: Boolean
        get() = _state.value.let {
            it !is CallState.Idle && it !is CallState.Ended && it !is CallState.Failed
        }

    fun startCall(pairing: StoredPairing, number: String) {
        if (isActive) return
        _state.value = CallState.Preparing(number)
        scope.launch {
            try {
                val invite = withContext(ioDispatcher) {
                    inviteProvider(pairing)
                }
                val payload = InviteParser.parseInviteUrl(invite.url)
                    ?: error("邀请解析失败（协议版本或格式不符）")
                val s = sessionFactory()
                session = s
                eventJob = scope.launch { s.events.collect { handleEvent(number, it) } }
                onForeground(true)
                s.connect(payload.url, payload.token)
                _state.value = CallState.WaitingMedia(number)
                s.sendCommand(Signaling.encodeDial(number, UUID.randomUUID().toString()))
            } catch (e: Exception) {
                cleanup()
                _state.value = CallState.Failed(number, e.message ?: "拨号失败")
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
        when (event) {
            is SessionEvent.Edge -> when (val e = event.event) {
                is Signaling.Event.RemoteCall -> when (e.status) {
                    "dialing" -> _state.value = CallState.Dialing(number)
                    "connected" -> _state.value = CallState.InCall(number)
                    "ended", "hangup", "failed" -> {
                        cleanup()
                        _state.value = CallState.Ended(number, e.status)
                    }
                    else -> Unit // 未知状态透传给日志层，不破坏状态机
                }
                is Signaling.Event.Status -> Unit // 生命周期提示，UI 可选展示
            }
            is SessionEvent.Disconnected -> {
                if (isActive) {
                    cleanup()
                    _state.value = CallState.Ended(number, event.reason)
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

    private fun cleanup() {
        eventJob?.cancel()
        eventJob = null
        session?.disconnect()
        session = null
        onForeground(false)
    }
}
