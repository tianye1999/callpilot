package ai.bondings.callpilot.ui

import ai.bondings.callpilot.pairing.CredentialStore
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.GatewayClient
import ai.bondings.callpilot.protocol.InviteParser
import android.os.Build
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
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
 * 配对页：填网关地址 + 配对码（或直接粘贴 `...#pair=XXXX-XXXX` 链接）。
 * 桌面端「远程拨号」设置里生成配对码（TTL 5 分钟，一次性）。
 */
@Composable
fun PairScreen(store: CredentialStore, onPaired: (StoredPairing) -> Unit) {
    var gatewayUrl by remember { mutableStateOf("") }
    var code by remember { mutableStateOf("") }
    var deviceName by remember { mutableStateOf(Build.MODEL ?: "Android") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("配对到 CallPilot Edge", style = MaterialTheme.typography.headlineSmall)
        Text(
            "在电脑端 CallPilot 设置生成配对码，5 分钟内在此完成配对。",
            style = MaterialTheme.typography.bodyMedium,
        )
        OutlinedTextField(
            value = gatewayUrl,
            onValueChange = { gatewayUrl = it.trim() },
            label = { Text("网关地址（https://…）") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = code,
            onValueChange = { input ->
                // 支持整段粘贴配对链接：…#pair=XXXX-XXXX
                val fromLink = InviteParser.parsePairingCode(InviteParser.fragmentOf(input))
                code = fromLink ?: input.trim().uppercase()
            },
            label = { Text("配对码（XXXX-XXXX）") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = deviceName,
            onValueChange = { deviceName = it },
            label = { Text("设备名称") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        if (busy) {
            CircularProgressIndicator()
        } else {
            Button(
                onClick = {
                    error = null
                    busy = true
                    scope.launch(Dispatchers.IO) {
                        try {
                            val client = GatewayClient(gatewayUrl)
                            val result = client.pair(code, deviceName)
                            val pairing = StoredPairing(gatewayUrl, deviceName, result.credential)
                            store.save(pairing)
                            onPaired(pairing)
                        } catch (e: Exception) {
                            error = e.message ?: "配对失败"
                        } finally {
                            busy = false
                        }
                    }
                },
                enabled = gatewayUrl.startsWith("http") && code.isNotBlank(),
                modifier = Modifier.fillMaxWidth(),
            ) { Text("配对") }
        }
    }
}
