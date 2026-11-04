package ai.bondings.callpilot.media

import ai.bondings.callpilot.protocol.IceServer
import ai.bondings.callpilot.protocol.Signaling
import ai.bondings.callpilot.protocol.Topics
import ai.bondings.callpilot.call.OutboundTiming
import ai.bondings.callpilot.call.OutboundTiming.Mark
import android.content.Context
import android.media.AudioManager
import android.util.Log
import java.nio.ByteBuffer
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import livekit.org.webrtc.AudioSource
import livekit.org.webrtc.AudioTrack
import livekit.org.webrtc.DataChannel
import livekit.org.webrtc.IceCandidate
import livekit.org.webrtc.MediaConstraints
import livekit.org.webrtc.MediaStream
import livekit.org.webrtc.PeerConnection
import livekit.org.webrtc.PeerConnectionFactory
import livekit.org.webrtc.SdpObserver
import livekit.org.webrtc.SessionDescription
import livekit.org.webrtc.audio.JavaAudioDeviceModule

/**
 * 来电接管直连（Fastpath V2）：手机与 Edge 经 Cloudflare TURN 直接建 WebRTC，
 * 控制信令走 `callpilot.control` 数据通道，与 iOS WebRtcDirectTransport 同一套协议。
 *
 * 直连失败时在同一 claim 下回落 LiveKit：`connect()` 关掉对等连接后交给
 * `fallbackFactory()` 建的房间会话，事件流合并，调用方不需要换对象。
 * 用 LiveKit SDK 自带（已改名为 livekit.org.webrtc）的 libwebrtc，不另引第二份原生库。
 */
class WebRtcDirectSession(
    context: Context,
    private val scope: CoroutineScope,
    private val fallbackFactory: () -> RemoteSession,
) : DirectCapableSession {

    private companion object {
        const val TAG = "CallPilotDirect"
        const val MAX_SDP_BYTES = 16 * 1024
        const val MAX_CONTROL_BYTES = 4096
        // 都留在 CallManager 的阶段上限（20 s）之内，超时由本层先报错、上层回落。
        const val GATHER_TIMEOUT_MS = 15_000L
        const val CONNECT_TIMEOUT_MS = 15_000L
        const val TRACK_ID = "phone-uplink"
        const val STREAM_ID = "callpilot"
    }

    private val appContext = context.applicationContext
    private val audioManager = appContext.getSystemService(AudioManager::class.java)
    private var factory: PeerConnectionFactory? = null
    private var audioModule: JavaAudioDeviceModule? = null
    private var peer: PeerConnection? = null
    private var source: AudioSource? = null
    private var track: AudioTrack? = null
    private var channel: DataChannel? = null
    private var fallback: RemoteSession? = null
    private var fallbackJob: Job? = null
    private val gathered = CompletableDeferred<Unit>()
    private val gatherComplete = CompletableDeferred<Unit>()
    private val connected = CompletableDeferred<Unit>()
    private val channelOpen = CompletableDeferred<Unit>()
    // native 对象的每一次调用、释放都在这一个线程上发起（挂断的 closeDirect 也切到它）：
    // 持引用的本地变量不可能跨越释放边界，不存在「检查完、还没调用」的窗口。
    @OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
    private val native = Dispatchers.Default.limitedParallelism(1)
    // 音频设置与回落会话的发布仍用普通锁（都是不挂起的短操作）。
    private val nativeLock = Any()
    private var audioOwned = false
    private var previousAudioMode = AudioManager.MODE_NORMAL
    private var previousSpeakerphone = false

    @Volatile
    private var directActive = false

    @Volatile
    private var closed = false

    @Volatile private var outboundTiming: OutboundTiming? = null

    override fun observeOutboundTiming(timing: OutboundTiming) { outboundTiming = timing }

    private val _events = MutableSharedFlow<SessionEvent>(extraBufferCapacity = 32)
    override val events: SharedFlow<SessionEvent> = _events

    private val observer = object : PeerConnection.Observer {
        override fun onSignalingChange(state: PeerConnection.SignalingState?) = Unit
        override fun onIceConnectionChange(state: PeerConnection.IceConnectionState?) = Unit
        override fun onIceConnectionReceivingChange(receiving: Boolean) = Unit
        override fun onIceGatheringChange(state: PeerConnection.IceGatheringState?) {
            if (state == PeerConnection.IceGatheringState.COMPLETE) {
                outboundTiming?.mark(Mark.ICE_COMPLETE)
                gathered.complete(Unit)
                gatherComplete.complete(Unit)
            }
        }

        override fun onIceCandidate(candidate: IceCandidate?) {
            // 与 iOS 一致：拿到第一个 relay 候选就可以发 offer，不等完整收集。
            if (candidate?.sdp?.contains(" typ relay") == true) {
                outboundTiming?.mark(Mark.RELAY_SEEN)
                gathered.complete(Unit)
            }
        }

        override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>?) = Unit
        override fun onAddStream(stream: MediaStream?) = Unit
        override fun onRemoveStream(stream: MediaStream?) = Unit
        override fun onDataChannel(channel: DataChannel?) = Unit
        override fun onRenegotiationNeeded() = Unit
        override fun onConnectionChange(state: PeerConnection.PeerConnectionState?) {
            Log.i(TAG, "peer state=$state")
            when (state) {
                PeerConnection.PeerConnectionState.CONNECTED -> connected.complete(Unit)
                PeerConnection.PeerConnectionState.FAILED,
                PeerConnection.PeerConnectionState.CLOSED,
                PeerConnection.PeerConnectionState.DISCONNECTED -> {
                    val reason = "direct_${state.name.lowercase()}"
                    if (!connected.isCompleted) connected.completeExceptionally(IllegalStateException(reason))
                    if (directActive) _events.tryEmit(SessionEvent.Disconnected(reason))
                }
                else -> Unit
            }
        }
    }

    private val channelObserver = object : DataChannel.Observer {
        override fun onBufferedAmountChange(previousAmount: Long) = Unit
        override fun onStateChange() {
            when (channel?.state()) {
                DataChannel.State.OPEN -> channelOpen.complete(Unit)
                DataChannel.State.CLOSED -> {
                    if (!channelOpen.isCompleted) channelOpen.completeExceptionally(IllegalStateException("channel_closed"))
                    if (directActive) _events.tryEmit(SessionEvent.Disconnected("channel_closed"))
                }
                else -> Unit
            }
        }

        override fun onMessage(buffer: DataChannel.Buffer?) {
            val data = buffer?.data ?: return
            if (buffer.binary || data.remaining() > MAX_CONTROL_BYTES) return
            val bytes = ByteArray(data.remaining()).also { data.get(it) }
            val event = runCatching { Signaling.decodeEvent(bytes.decodeToString()) }.getOrNull() ?: return
            _events.tryEmit(SessionEvent.Edge(event))
        }
    }

    override suspend fun prepareDirect(iceServers: List<IceServer>): String = withContext(native) {
        val pc = run {
            check(!closed && peer == null) { "direct session already used" }
            outboundTiming?.mark(Mark.FACTORY_START)
            PeerConnectionFactory.initialize(
                PeerConnectionFactory.InitializationOptions.builder(appContext).createInitializationOptions(),
            )
            val module = JavaAudioDeviceModule.builder(appContext)
                .setUseHardwareAcousticEchoCanceler(true)
                .setUseHardwareNoiseSuppressor(true)
                .createAudioDeviceModule()
            audioModule = module
            outboundTiming?.mark(Mark.ADM)
            val f = PeerConnectionFactory.builder().setAudioDeviceModule(module).createPeerConnectionFactory()
            factory = f
            outboundTiming?.mark(Mark.FACTORY)
            val config = PeerConnection.RTCConfiguration(
                iceServers.map { server ->
                    PeerConnection.IceServer.builder(server.urls).apply {
                        server.username?.let { setUsername(it) }
                        server.credential?.let { setPassword(it) }
                    }.createIceServer()
                },
            ).apply {
                sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
                continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_ONCE
            }
            val created = f.createPeerConnection(config, observer) ?: error("peer connection unavailable")
            peer = created
            val audioSource = f.createAudioSource(MediaConstraints()).also { source = it }
            val audio = f.createAudioTrack(TRACK_ID, audioSource).also { it.setEnabled(true) }
            track = audio
            created.addTrack(audio, listOf(STREAM_ID))
            val dc = created.createDataChannel(Topics.CONTROL, DataChannel.Init()) ?: error("data channel unavailable")
            dc.registerObserver(channelObserver)
            channel = dc
            outboundTiming?.mark(Mark.PEER_READY)
            created
        }
        val deadline = System.nanoTime() + GATHER_TIMEOUT_MS * 1_000_000
        fun remainingMs() = ((deadline - System.nanoTime()) / 1_000_000).coerceAtLeast(1)
        val offer = pc.awaitCreateOffer()
        outboundTiming?.mark(Mark.OFFER_CREATED)
        ensureOpen()
        pc.awaitSetLocalDescription(offer)
        outboundTiming?.mark(Mark.LOCAL_SET)
        ensureOpen()
        // 与 iOS 一致：第一个 relay 候选或收集完成即可；候选回调可能早于 localDescription 更新，
        // 这时在同一预算内继续等收集完成，绝不发一份没有候选的 SDP。
        withTimeoutOrNull(remainingMs()) { gathered.await() }
        ensureOpen()
        if (pc.localDescription?.description?.contains(" typ relay") != true &&
            pc.iceGatheringState() != PeerConnection.IceGatheringState.COMPLETE
        ) {
            awaitRelayPublication(
                gatherComplete,
                { ensureOpen(); pc.localDescription?.description?.contains(" typ relay") == true },
                { ensureOpen(); pc.iceGatheringState() == PeerConnection.IceGatheringState.COMPLETE },
                ::remainingMs,
                // This experiment is outbound-only; inbound preparation is unchanged.
                enabled = outboundTiming != null && DirectSetupExperiments.POLL_PUBLISHED_RELAY,
            )
        }
        ensureOpen()
        val local = pc.localDescription?.description ?: error("no local description")
        if (!local.contains(" typ relay") && pc.iceGatheringState() != PeerConnection.IceGatheringState.COMPLETE) {
            error("ICE gathering produced no relay candidate")
        }
        check(local.toByteArray().size <= MAX_SDP_BYTES) { "local SDP too large" }
        outboundTiming?.mark(Mark.GATHER_DONE)
        local
    }

    private fun ensureOpen() {
        check(!closed && peer != null) { "direct session closed" }
    }

    override suspend fun connectDirect(sdpAnswer: String): Unit = withContext(native) {
        require(sdpAnswer.isNotEmpty() && sdpAnswer.toByteArray().size <= MAX_SDP_BYTES) { "remote SDP invalid" }
        val pc = peer ?: error("prepareDirect first")
        ensureOpen()
        enterCallAudioMode()
        pc.awaitSetRemoteDescription(SessionDescription(SessionDescription.Type.ANSWER, sdpAnswer))
        ensureOpen()
        withTimeout(CONNECT_TIMEOUT_MS) {
            connected.await()
            channelOpen.await()
        }
        ensureOpen()
        directActive = true
        Log.i(TAG, "direct media connected")
        Unit
    }

    /**
     * 同一 claim 下回落 LiveKit：先放掉对等连接，再把事件流接到房间会话上。
     * 已关闭的会话拒绝启动；回落会话先发布再连接，中途 disconnect 能拿到它并断开。
     */
    override suspend fun connect(livekitUrl: String, token: String) {
        closeDirect()
        val fb = synchronized(nativeLock) {
            check(!closed) { "direct session closed" }
            fallback ?: fallbackFactory().also { fallback = it }
        }
        fallbackJob?.cancel()
        fallbackJob = scope.launch { fb.events.collect { _events.tryEmit(it) } }
        fb.connect(livekitUrl, token)
    }

    override suspend fun sendCommand(json: String) {
        synchronized(nativeLock) { fallback }?.let { return it.sendCommand(json) }
        val bytes = json.encodeToByteArray()
        require(bytes.size <= MAX_CONTROL_BYTES) { "control message too large" }
        withContext(native) {
            ensureOpen()
            val dc = channel ?: error("control channel unavailable")
            check(dc.send(DataChannel.Buffer(ByteBuffer.wrap(bytes), false))) { "control channel send failed" }
        }
    }

    @Suppress("DEPRECATION")
    override fun setSpeakerphone(enabled: Boolean) {
        synchronized(nativeLock) { fallback }?.let { it.setSpeakerphone(enabled); return }
        synchronized(nativeLock) { if (audioOwned) audioManager.isSpeakerphoneOn = enabled }
    }

    /** 释放在 native 线程上串行执行：正在进行的 native 调用先完成，之后的调用因 ensureOpen 不再碰已释放对象。 */
    override fun closeDirect() {
        directActive = false
        runBlocking(native) { closeDirectOnNativeThread() }
    }

    private fun closeDirectOnNativeThread() {
        // 取走引用后再释放：重复关闭拿不到同一个对象，不会重复 dispose。
        val (dc, pc, audio, audioSource, f, module) = run {
            val taken = NativeHandles(channel, peer, track, source, factory, audioModule)
            channel = null
            peer = null
            track = null
            source = null
            factory = null
            audioModule = null
            taken
        }
        runCatching { audio?.setEnabled(false) }
        dc?.let {
            runCatching { it.unregisterObserver() }
            runCatching { it.close() }
            runCatching { it.dispose() }
        }
        pc?.let {
            runCatching { it.close() }
            runCatching { it.dispose() }
        }
        runCatching { audio?.dispose() }
        runCatching { audioSource?.dispose() }
        runCatching { f?.dispose() }
        runCatching { module?.release() }
        if (synchronized(nativeLock) { fallback } == null) restoreAudioMode()
    }

    private data class NativeHandles(
        val channel: DataChannel?,
        val peer: PeerConnection?,
        val track: AudioTrack?,
        val source: AudioSource?,
        val factory: PeerConnectionFactory?,
        val audioModule: JavaAudioDeviceModule?,
    )

    override fun disconnect() {
        closed = true
        closeDirect()
        fallbackJob?.cancel()
        fallbackJob = null
        // 取走后再断：connect() 中途被挂断时也能断掉它刚发布的回落会话。
        val fb = synchronized(nativeLock) { fallback.also { fallback = null } }
        fb?.disconnect()
        restoreAudioMode()
    }

    /** 所有权判定与系统写操作在同一把锁内，恢复不会与进入交错。 */
    @Suppress("DEPRECATION")
    private fun enterCallAudioMode() = synchronized(nativeLock) {
        if (audioOwned || closed) return
        previousAudioMode = audioManager.mode
        previousSpeakerphone = audioManager.isSpeakerphoneOn
        audioOwned = true
        audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
        audioManager.isSpeakerphoneOn = false
    }

    /** 只恢复自己改过的设置，且只恢复一次；没进过通话模式就什么都不碰（别人的音频会话不归我们管）。 */
    @Suppress("DEPRECATION")
    private fun restoreAudioMode() = synchronized(nativeLock) {
        if (!audioOwned) return
        audioOwned = false
        audioManager.mode = previousAudioMode
        audioManager.isSpeakerphoneOn = previousSpeakerphone
    }

    private suspend fun PeerConnection.awaitCreateOffer(): SessionDescription {
        val result = CompletableDeferred<SessionDescription>()
        createOffer(
            object : SdpObserver {
                override fun onCreateSuccess(description: SessionDescription?) {
                    if (description != null) result.complete(description) else result.completeExceptionally(IllegalStateException("empty offer"))
                }

                override fun onSetSuccess() = Unit
                override fun onCreateFailure(error: String?) {
                    result.completeExceptionally(IllegalStateException(error ?: "createOffer failed"))
                }

                override fun onSetFailure(error: String?) = Unit
            },
            MediaConstraints(),
        )
        return result.await()
    }

    private suspend fun PeerConnection.awaitSetLocalDescription(description: SessionDescription) {
        val result = CompletableDeferred<Unit>()
        setLocalDescription(SetObserver(result), description)
        result.await()
    }

    private suspend fun PeerConnection.awaitSetRemoteDescription(description: SessionDescription) {
        val result = CompletableDeferred<Unit>()
        setRemoteDescription(SetObserver(result), description)
        result.await()
    }

    private class SetObserver(private val result: CompletableDeferred<Unit>) : SdpObserver {
        override fun onCreateSuccess(description: SessionDescription?) = Unit
        override fun onSetSuccess() {
            result.complete(Unit)
        }

        override fun onCreateFailure(error: String?) = Unit
        override fun onSetFailure(error: String?) {
            result.completeExceptionally(IllegalStateException(error ?: "setDescription failed"))
        }
    }
}
