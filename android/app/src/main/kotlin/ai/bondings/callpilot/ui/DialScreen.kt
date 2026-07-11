package ai.bondings.callpilot.ui

import ai.bondings.callpilot.pairing.CredentialStore
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.DeviceStatus
import ai.bondings.callpilot.protocol.GatewayClient
import ai.bondings.callpilot.protocol.Validation
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/**
 * 拨号页（M3：协议联测形态）。
 * 「创建会话」验证 /api/session 链路；真实媒体拨号在 M4 接入 LiveKit 后启用。
 */
@Composable
fun DialScreen(pairing: StoredPairing, store: CredentialStore, onUnpaired: () -> Unit) {
    val client = remember(pairing) {
        GatewayClient(pairing.gatewayUrl).also { it.credential = pairing.credential }
    }
    var status by remember { mutableStateOf<DeviceStatus?>(null) }
    var number by remember { mutableStateOf("") }
    var message by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    LaunchedEffect(client) {
        launch(Dispatchers.IO) {
            try {
                status = client.deviceStatus()
            } catch (e: Exception) {
                message = "获取线路状态失败：${e.message}"
            }
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("CallPilot 远程拨号", style = MaterialTheme.typography.headlineSmall)
        Text(
            status?.let {
                val edge = if (it.edgeEnabled) "已启用" else "未启用"
                "设备：${pairing.displayName} · 远程拨号：$edge"
            } ?: "线路状态获取中…",
            style = MaterialTheme.typography.bodyMedium,
        )
        OutlinedTextField(
            value = number,
            onValueChange = { number = it.filter { c -> c.isDigit() || c == '+' || c == '*' || c == '#' } },
            label = { Text("号码（真机测试只拨 10000）") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Button(
            onClick = {
                message = null
                scope.launch(Dispatchers.IO) {
                    try {
                        val invite = client.createSession()
                        message = "会话已创建：${invite.sessionId.take(8)}…（媒体拨号将在 M4 接入）"
                    } catch (e: Exception) {
                        message = "创建会话失败：${e.message}"
                    }
                }
            },
            enabled = Validation.isValidNumber(number),
            modifier = Modifier.fillMaxWidth(),
        ) { Text("创建会话（协议联测）") }
        message?.let { Text(it, style = MaterialTheme.typography.bodyMedium) }
        OutlinedButton(
            onClick = {
                scope.launch(Dispatchers.IO) {
                    try {
                        client.unpair()
                    } catch (_: Exception) {
                        // 网关不可达时也允许本地解除
                    }
                    store.clear()
                    onUnpaired()
                }
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("解除配对") }
    }
}
