package ai.bondings.callpilot.ui

import ai.bondings.callpilot.call.CallManager
import ai.bondings.callpilot.pairing.CredentialStore
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.DeviceStatus
import ai.bondings.callpilot.protocol.GatewayClient
import ai.bondings.callpilot.protocol.Validation
import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/** 拨号页：线路状态 + 号码 + 真实拨号（先要麦克风权限）。 */
@Composable
fun DialScreen(
    pairing: StoredPairing,
    store: CredentialStore,
    manager: CallManager,
    onUnpaired: () -> Unit,
) {
    val context = LocalContext.current
    val client = remember(pairing) {
        GatewayClient(pairing.gatewayUrl).also { it.credential = pairing.credential }
    }
    var status by remember { mutableStateOf<DeviceStatus?>(null) }
    var number by remember { mutableStateOf("") }
    var message by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    val micPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            manager.startCall(pairing, number)
        } else {
            message = "需要麦克风权限才能通话"
        }
    }

    fun dial() {
        message = null
        val granted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.RECORD_AUDIO,
        ) == PackageManager.PERMISSION_GRANTED
        if (granted) manager.startCall(pairing, number) else micPermission.launch(Manifest.permission.RECORD_AUDIO)
    }

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
            onClick = ::dial,
            enabled = Validation.isValidNumber(number),
            modifier = Modifier.fillMaxWidth(),
        ) { Text("拨号（经远端 SIM）") }
        message?.let { Text(it, color = MaterialTheme.colorScheme.error) }
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
