package ai.bondings.callpilot.media

import ai.bondings.callpilot.protocol.IceServer
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

/** 支持直连的会话：先 TURN 建对等连接产出 offer，再用 Edge 的 answer 连通；失败后 `connect()` 回落 LiveKit。 */
interface DirectCapableSession : RemoteSession {
    /** 建对等连接与控制数据通道，返回含 relay 候选的本地 SDP（不超过 16 KiB）。 */
    suspend fun prepareDirect(iceServers: List<IceServer>): String

    /** 设置 Edge 的 answer，等到 ICE 连通且控制通道打开。 */
    suspend fun connectDirect(sdpAnswer: String)

    /** 放弃直连（回落前或收尾），不影响已建立的 LiveKit 回落会话。 */
    fun closeDirect()
}

sealed interface SessionEvent {
    /** Edge 发来的协议事件。 */
    data class Edge(val event: Signaling.Event) : SessionEvent

    /** 房间断开（网络中断 / Edge 收尾 / 主动断开）。 */
    data class Disconnected(val reason: String) : SessionEvent
}
