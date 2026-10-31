package ai.bondings.callpilot.call

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class OfferWatchBootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        // specialUse 不在 Android 15 的开机禁用类型中；不启动 microphone 服务。
        // 凭证在加密存储，只接收首次解锁后的 BOOT_COMPLETED。
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) OfferWatchService.startIfPaired(context)
    }
}
