package ai.bondings.callpilot.ui

import ai.bondings.callpilot.call.CallManager
import ai.bondings.callpilot.call.CallState
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay

/** 通话页：号码 + 计时 + 12 键 DTMF（导航 IVR）+ 扬声器 + 挂断。 */
@Composable
fun CallScreen(state: CallState, manager: CallManager) {
    var speaker by remember { mutableStateOf(false) }
    var seconds by remember { mutableIntStateOf(0) }
    val inCall = state is CallState.InCall
    LaunchedEffect(inCall) {
        if (inCall) {
            seconds = 0
            while (true) {
                delay(1_000)
                seconds++
            }
        }
    }

    val (number, statusText) = when (state) {
        is CallState.Preparing -> state.number to "正在创建会话…"
        is CallState.WaitingMedia -> state.number to "媒体通道建立中…"
        is CallState.Dialing -> state.number to "拨号中…"
        is CallState.InCall -> state.number to "%02d:%02d".format(seconds / 60, seconds % 60)
        is CallState.Ended -> state.number to "通话结束（${state.reason}）"
        is CallState.Failed -> state.number to "失败：${state.reason}"
        CallState.Idle -> "" to ""
    }
    // 通话活跃全程都给键盘：客服 IVR 从会话建立到接通任一阶段都可能要按键，
    // 即使状态事件延迟，用户也不能没有键盘可按
    val canDtmf = state !is CallState.Idle &&
        state !is CallState.Ended &&
        state !is CallState.Failed

    Column(
        modifier = Modifier
            .fillMaxSize()
            // 小屏/横屏/大字体下内容超高时可滚动，保证挂断键永远可达
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 20.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Spacer(Modifier.height(40.dp))
        Text(number, fontSize = 34.sp, fontFamily = FontFamily.Monospace)
        Spacer(Modifier.height(8.dp))
        Text(
            statusText,
            style = MaterialTheme.typography.titleMedium,
            color = if (state is CallState.Failed) MaterialTheme.colorScheme.error
            else MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text("经远端 SIM 通话", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline)

        Spacer(Modifier.height(24.dp))
        if (canDtmf) {
            Keypad(onKey = { manager.sendDtmf(it) }, keySize = 64.dp, showLetters = false)
            Spacer(Modifier.height(14.dp))
            FilterChip(
                selected = speaker,
                onClick = { speaker = !speaker; manager.setSpeakerphone(speaker) },
                label = { Text(if (speaker) "🔊 扬声器" else "🔈 听筒") },
            )
        }

        Spacer(Modifier.height(28.dp))
        when (state) {
            is CallState.Ended, is CallState.Failed ->
                GradientButton(text = "返回", onClick = { manager.reset() })
            CallState.Idle -> Unit
            else -> Box(
                modifier = Modifier
                    .size(70.dp)
                    .background(Color(0xFFE5484D), CircleShape)
                    .clickable { manager.hangup() },
                contentAlignment = Alignment.Center,
            ) { Text("✕", color = Color.White, fontSize = 26.sp) }
        }
        Spacer(Modifier.height(36.dp))
    }
}
