package ai.bondings.callpilot.media

import ai.bondings.callpilot.protocol.Signaling
import ai.bondings.callpilot.protocol.Topics
import android.content.Context
import com.twilio.audioswitch.AudioDevice
import io.livekit.android.AudioOptions
import io.livekit.android.AudioType
import io.livekit.android.LiveKit
import io.livekit.android.LiveKitOverrides
import io.livekit.android.audio.AudioSwitchHandler
import io.livekit.android.events.RoomEvent
import io.livekit.android.events.collect
import io.livekit.android.room.Room
import io.livekit.android.room.track.DataPublishReliability
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.launch

/** LiveKit 实现：通话音频走 CallAudioType（听筒路由 + 通信模式 AEC）。 */
class LiveKitSession(
    context: Context,
    private val scope: CoroutineScope,
) : RemoteSession {

    private val appContext = context.applicationContext
    private val audioHandler = AudioSwitchHandler(appContext)
    private val room: Room = LiveKit.create(
        appContext = appContext,
        overrides = LiveKitOverrides(
            audioOptions = AudioOptions(
                audioOutputType = AudioType.CallAudioType(),
                audioHandler = audioHandler,
            ),
        ),
    )
    private var eventJob: Job? = null

    private val _events = MutableSharedFlow<SessionEvent>(extraBufferCapacity = 32)
    override val events: SharedFlow<SessionEvent> = _events

    override suspend fun connect(livekitUrl: String, token: String) {
        eventJob = scope.launch {
            room.events.collect { event ->
                when (event) {
                    is RoomEvent.DataReceived -> {
                        if (event.topic == Topics.STATUS) {
                            Signaling.decodeEvent(event.data.decodeToString())
                                ?.let { _events.tryEmit(SessionEvent.Edge(it)) }
                        }
                    }
                    is RoomEvent.Disconnected ->
                        _events.tryEmit(
                            SessionEvent.Disconnected(event.reason?.name ?: "disconnected")
                        )
                    else -> Unit
                }
            }
        }
        room.connect(livekitUrl, token)
        room.localParticipant.setMicrophoneEnabled(true)
    }

    override suspend fun sendCommand(json: String) {
        room.localParticipant.publishData(
            data = json.encodeToByteArray(),
            reliability = DataPublishReliability.RELIABLE,
            topic = Topics.CONTROL,
        )
    }

    override fun setSpeakerphone(enabled: Boolean) {
        val devices = audioHandler.availableAudioDevices
        val target = if (enabled) {
            devices.firstOrNull { it is AudioDevice.Speakerphone }
        } else {
            // 优先蓝牙/有线耳机，其次听筒
            devices.firstOrNull { it is AudioDevice.BluetoothHeadset }
                ?: devices.firstOrNull { it is AudioDevice.WiredHeadset }
                ?: devices.firstOrNull { it is AudioDevice.Earpiece }
        }
        target?.let { audioHandler.selectDevice(it) }
    }

    override fun disconnect() {
        eventJob?.cancel()
        eventJob = null
        room.disconnect()
    }
}
