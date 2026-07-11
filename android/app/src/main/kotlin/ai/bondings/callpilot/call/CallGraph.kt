package ai.bondings.callpilot.call

import ai.bondings.callpilot.media.LiveKitSession
import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

/** 进程级单例装配：CallManager 存活期跨 Activity 重建，通话不因转屏丢状态。 */
object CallGraph {
    private val sessionScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    @Volatile
    private var manager: CallManager? = null

    fun manager(context: Context): CallManager {
        val app = context.applicationContext
        return manager ?: synchronized(this) {
            manager ?: CallManager(
                sessionFactory = { LiveKitSession(app, sessionScope) },
                onForeground = { active ->
                    if (active) CallService.start(app) else CallService.stop(app)
                },
            ).also { manager = it }
        }
    }
}
