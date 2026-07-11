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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

/** 通话页 v2：大号显示 + 通话计时 + 圆形 DTMF + 红色挂断。 */
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

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(horizontal = 24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Spacer(Modifier.height(64.dp))
        Text(
            number,
            style = MaterialTheme.typography.displaySmall,
            fontFamily = FontFamily.Monospace,
        )
        Spacer(Modifier.height(10.dp))
        Text(
            statusText,
            style = MaterialTheme.typography.titleMedium,
            color = if (state is CallState.Failed) MaterialTheme.colorScheme.error
            else MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(12.dp))
        Text(
            "经远端 SIM 通话",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.outline,
        )

        Spacer(Modifier.weight(1f))

        if (state is CallState.InCall || state is CallState.Dialing) {
            DtmfPad(onKey = { manager.sendDtmf(it) })
            Spacer(Modifier.height(20.dp))
            FilterChip(
                selected = speaker,
                onClick = {
                    speaker = !speaker
                    manager.setSpeakerphone(speaker)
                },
                label = { Text(if (speaker) "🔊 扬声器" else "🔈 听筒") },
            )
            Spacer(Modifier.height(24.dp))
        }

        when (state) {
            is CallState.Ended, is CallState.Failed -> {
                GradientButton(text = "返回", onClick = { manager.reset() })
                Spacer(Modifier.height(40.dp))
            }
            CallState.Idle -> Unit
            else -> {
                // 红色圆形挂断键（Material 惯例：话筒旋转 135°）
                Box(
                    modifier = Modifier
                        .size(72.dp)
                        .clip(CircleShape)
                        .background(Color(0xFFE5484D))
                        .clickable { manager.hangup() },
                    contentAlignment = Alignment.Center,
                ) {
                    Icon(
                        Icons.Filled.Call,
                        contentDescription = "挂断",
                        tint = Color.White,
                        modifier = Modifier
                            .size(34.dp)
                            .rotate(135f),
                    )
                }
                Spacer(Modifier.height(40.dp))
            }
        }
    }
}

@Composable
private fun DtmfPad(onKey: (String) -> Unit) {
    val rows = listOf(
        listOf("1", "2", "3"),
        listOf("4", "5", "6"),
        listOf("7", "8", "9"),
        listOf("*", "0", "#"),
    )
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        rows.forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                row.forEach { key ->
                    OutlinedButton(
                        onClick = { onKey(key) },
                        shape = CircleShape,
                        contentPadding = androidx.compose.foundation.layout.PaddingValues(0.dp),
                        modifier = Modifier.size(60.dp),
                    ) {
                        Text(key, style = MaterialTheme.typography.titleLarge)
                    }
                }
            }
        }
    }
}
