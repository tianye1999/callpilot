package ai.bondings.callpilot.ui

import ai.bondings.callpilot.pairing.CredentialStore
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.PairingLink
import ai.bondings.callpilot.protocol.PairingNegotiator
import ai.bondings.callpilot.protocol.PairingProtocol
import android.os.Build
import androidx.compose.foundation.background
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
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/**
 * 配对页 v2：粘贴桌面端的配对链接即可——链接同时携带网关地址与配对码，
 * 用户无需手填网关（docs/remote-protocol.md：静态页与 /api 接口同源）。
 */
@Composable
fun PairScreen(store: CredentialStore, onPaired: (StoredPairing) -> Unit) {
    var gatewayBase by remember { mutableStateOf(store.loadLastGateway()) }
    var protocol by remember { mutableStateOf<PairingProtocol?>(null) }
    var code by remember { mutableStateOf("") }
    var deviceName by remember { mutableStateOf(Build.MODEL ?: "Android") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var showManualGateway by remember { mutableStateOf(false) }
    val clipboard = LocalClipboardManager.current
    val scope = rememberCoroutineScope()

    fun applyParsed(text: String): Boolean {
        val parsed = PairingLink.parse(text)
        if (parsed.isEmpty) return false
        parsed.gatewayBase?.let {
            gatewayBase = it
            protocol = null
        }
        parsed.code?.let { code = it }
        error = null
        return true
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 28.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Spacer(Modifier.height(48.dp))
        Box(
            modifier = Modifier
                .size(76.dp)
                .clip(CircleShape)
                .background(Brand.gradient),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                Icons.Filled.Call,
                contentDescription = null,
                tint = Color.White,
                modifier = Modifier.size(38.dp),
            )
        }
        Spacer(Modifier.height(18.dp))
        Text("配对到 CallPilot Edge", style = MaterialTheme.typography.headlineSmall)
        Spacer(Modifier.height(8.dp))
        Text(
            "在电脑端 CallPilot 点「配对手机」，复制配对链接后回到这里粘贴。",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(20.dp))

        OutlinedButton(
            onClick = {
                val clip = clipboard.getText()?.text.orEmpty()
                if (!applyParsed(clip)) error = "剪贴板里没有可识别的配对链接或配对码"
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("📋  粘贴配对链接") }

        Spacer(Modifier.height(6.dp))
        val gateway = gatewayBase
        if (gateway != null) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    Modifier
                        .size(8.dp)
                        .clip(CircleShape)
                        .background(Brand.Green),
                )
                Spacer(Modifier.size(6.dp))
                Text(
                    "网关 ${gateway.removePrefix("https://").removePrefix("http://")}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        } else {
            Text(
                "粘贴链接后自动识别网关地址",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }

        Spacer(Modifier.height(22.dp))
        Text(
            "配对码",
            style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.align(Alignment.Start),
        )
        Spacer(Modifier.height(8.dp))
        CodeInput(
            value = code,
            onValueChange = { raw ->
                if (raw.contains("://")) {
                    if (!applyParsed(raw.trim())) error = "无法识别粘贴的内容"
                } else {
                    code = raw.filter { it.isLetterOrDigit() }.uppercase().take(8)
                }
            },
            enabled = !busy,
        )

        Spacer(Modifier.height(18.dp))
        SingleChoiceSegmentedButtonRow(modifier = Modifier.fillMaxWidth()) {
            val options: List<Pair<PairingProtocol?, String>> = listOf(
                null to "自动",
                PairingProtocol.HOSTED to "云托管",
                PairingProtocol.TUNNEL to "Tunnel",
            )
            options.forEachIndexed { index, (value, label) ->
                SegmentedButton(
                    selected = protocol == value,
                    onClick = { protocol = value },
                    shape = SegmentedButtonDefaults.itemShape(index, options.size),
                    label = { Text(label) },
                )
            }
        }

        Spacer(Modifier.height(18.dp))
        OutlinedTextField(
            value = deviceName,
            onValueChange = { deviceName = it },
            label = { Text("设备名称") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )

        error?.let {
            Spacer(Modifier.height(10.dp))
            Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
        }

        Spacer(Modifier.height(18.dp))
        GradientButton(
            text = "配对",
            busy = busy,
            enabled = code.length == 8 && gateway != null && deviceName.isNotBlank(),
            onClick = {
                val base = gatewayBase ?: return@GradientButton
                error = null
                busy = true
                scope.launch(Dispatchers.IO) {
                    try {
                        val name = deviceName.trim()
                        val formattedCode = PairingLink.formatCode(code)
                        val result = PairingNegotiator(base).pair(
                            formattedCode,
                            name,
                            preferredProtocol = protocol,
                        )
                        val pairing = StoredPairing(
                            gatewayUrl = base,
                            displayName = name,
                            credential = result.credential,
                            protocol = result.protocol,
                            edgeId = result.edgeId,
                        )
                        store.save(pairing)
                        onPaired(pairing)
                    } catch (e: Exception) {
                        error = e.message ?: "配对失败"
                    } finally {
                        busy = false
                    }
                }
            },
        )

        TextButton(onClick = { showManualGateway = !showManualGateway }) {
            Text(
                "手动填写网关地址",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
        if (showManualGateway) {
            OutlinedTextField(
                value = gatewayBase.orEmpty(),
                onValueChange = { gatewayBase = it.trim().ifEmpty { null } },
                label = { Text("网关地址（https://…）") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
        }
        Spacer(Modifier.height(24.dp))
    }
}
