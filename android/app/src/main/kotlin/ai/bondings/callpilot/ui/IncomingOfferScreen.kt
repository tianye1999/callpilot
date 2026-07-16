package ai.bondings.callpilot.ui

import android.content.Context
import android.media.RingtoneManager
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp

internal data class IncomingOfferActionColors(val container: Color, val content: Color)

internal object IncomingOfferColors {
    val reject = IncomingOfferActionColors(container = Color(0xFFB3261E), content = Color.White)
    val accept = IncomingOfferActionColors(container = Color(0xFF1B873B), content = Color.White)
}

/**
 * #95 inbound takeover：AI 请求把进行中的来电转给机主。
 * 前台全屏卡：铃声 + 振动 + 接听/拒绝。仅在 App 前台且空闲时展示（MVP 边界）。
 */
@Composable
fun IncomingOfferScreen(
    onAccept: () -> Unit,
    onDecline: () -> Unit,
) {
    val context = LocalContext.current

    DisposableEffect(Unit) {
        val ringtone = runCatching {
            RingtoneManager.getRingtone(
                context,
                RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE),
            )?.also { it.play() }
        }.getOrNull()
        val vibrator = vibratorOf(context)?.also {
            runCatching {
                it.vibrate(
                    VibrationEffect.createWaveform(longArrayOf(0, 600, 500), 0),
                )
            }
        }
        onDispose {
            runCatching { ringtone?.stop() }
            runCatching { vibrator?.cancel() }
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(28.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text("来电转接请求", style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.height(14.dp))
        Text(
            "AI 正在接听一通来电，判断需要你本人处理。\n接听后通话将转到这台手机。",
            style = MaterialTheme.typography.bodyLarge,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(44.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(20.dp)) {
            Button(
                onClick = onDecline,
                modifier = Modifier.weight(1f).height(64.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = IncomingOfferColors.reject.container,
                    contentColor = IncomingOfferColors.reject.content,
                ),
            ) { Text("拒绝", style = MaterialTheme.typography.titleMedium) }
            Button(
                onClick = onAccept,
                modifier = Modifier.weight(1f).height(64.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = IncomingOfferColors.accept.container,
                    contentColor = IncomingOfferColors.accept.content,
                ),
            ) { Text("接听", style = MaterialTheme.typography.titleMedium) }
        }
        Spacer(Modifier.height(18.dp))
        Text(
            "拒绝后 AI 会继续处理这通电话。",
            style = MaterialTheme.typography.bodySmall,
        )
    }
}

private fun vibratorOf(context: Context): Vibrator? = if (Build.VERSION.SDK_INT >= 31) {
    (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager)?.defaultVibrator
} else {
    @Suppress("DEPRECATION")
    context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
}
