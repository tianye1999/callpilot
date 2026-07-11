package ai.bondings.callpilot.media

import ai.bondings.callpilot.protocol.Signaling
import kotlinx.coroutines.flow.SharedFlow

/**
 * 远程媒体会话抽象：连接 LiveKit 房间、发布麦克风、收发信令。
 * 抽象出接口是为了让 call/ 层状态机可以用 fake 做 JVM 单测。
 */
interface RemoteSession {
    /** Edge 状态事件 + 会话层事件（断连）。 */
    val events: SharedFlow<SessionEvent>

    /** 连接房间并发布麦克风轨（media-ready 的前置）。 */
    suspend fun connect(livekitUrl: String, token: String)

    /** 经 reliable data packet 发送控制命令（JSON 原文）。 */
    suspend fun sendCommand(json: String)

    /** 扬声器 / 听筒切换。 */
    fun setSpeakerphone(enabled: Boolean)

    fun disconnect()
}

sealed interface SessionEvent {
    /** Edge 发来的协议事件。 */
    data class Edge(val event: Signaling.Event) : SessionEvent

    /** 房间断开（网络中断 / Edge 收尾 / 主动断开）。 */
    data class Disconnected(val reason: String) : SessionEvent
}
