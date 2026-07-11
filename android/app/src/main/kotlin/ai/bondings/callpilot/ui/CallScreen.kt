package ai.bondings.callpilot.ui

import ai.bondings.callpilot.call.CallManager
import ai.bondings.callpilot.call.CallState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/** 通话页：状态 + DTMF 键盘 + 扬声器切换 + 挂断。 */
@Composable
fun CallScreen(state: CallState, manager: CallManager) {
    var speaker by remember { mutableStateOf(false) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        val (number, statusText) = when (state) {
            is CallState.Preparing -> state.number to "正在创建会话…"
            is CallState.WaitingMedia -> state.number to "媒体通道建立中…"
            is CallState.Dialing -> state.number to "拨号中…"
            is CallState.InCall -> state.number to "通话中（经远端 SIM）"
            is CallState.Ended -> state.number to "通话结束（${state.reason}）"
            is CallState.Failed -> state.number to "失败：${state.reason}"
            CallState.Idle -> "" to ""
        }
        Text(number, style = MaterialTheme.typography.headlineMedium)
        Text(statusText, style = MaterialTheme.typography.bodyLarge)

        if (state is CallState.InCall || state is CallState.Dialing) {
            DtmfPad(onKey = { manager.sendDtmf(it) })
            OutlinedButton(
                onClick = {
                    speaker = !speaker
                    manager.setSpeakerphone(speaker)
                },
                modifier = Modifier.fillMaxWidth(),
            ) { Text(if (speaker) "切回听筒" else "扬声器") }
        }

        when (state) {
            is CallState.Ended, is CallState.Failed -> {
                Button(onClick = { manager.reset() }, modifier = Modifier.fillMaxWidth()) {
                    Text("返回")
                }
            }
            CallState.Idle -> Unit
            else -> {
                Button(
                    onClick = { manager.hangup() },
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.error,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("挂断") }
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
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        rows.forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                row.forEach { key ->
                    OutlinedButton(onClick = { onKey(key) }) { Text(key) }
                }
            }
        }
    }
}
