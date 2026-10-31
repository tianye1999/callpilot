package ai.bondings.callpilot.protocol

import java.util.concurrent.TimeUnit
import okhttp3.OkHttpClient

/**
 * 全 App 共用一个 OkHttpClient。
 *
 * 之前每个页面各自 `OkHttpClient()`：切回拨号页就要重新 TLS 握手，且默认无整体超时，
 * 锁屏后被系统静默断掉的连接要等读超时才放弃，线路状态长时间停在「获取中」。
 * 共用一个连接池让切页面复用连接；整体超时兜住卡死的请求。
 */
object HttpClients {
    val shared: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(12, TimeUnit.SECONDS)
        .writeTimeout(12, TimeUnit.SECONDS)
        .callTimeout(20, TimeUnit.SECONDS)
        .build()

    /** 锁屏 / 切网络后池里的连接可能已失效：请求失败时清掉，下一次重新建连。 */
    fun dropPooledConnections() {
        shared.connectionPool.evictAll()
    }
}
