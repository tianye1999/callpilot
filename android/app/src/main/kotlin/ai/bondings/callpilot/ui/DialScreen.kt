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
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/** 拨号页 v3：状态卡片 + 大号显示 + 自绘 12 键键盘（免系统输入法）+ 绿色拨号键。 */
@OptIn(ExperimentalFoundationApi::class)
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
        if (granted) manager.startCall(pairing, number) else message = "需要麦克风权限才能通话"
    }

    fun dial() {
        message = null
        val granted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.RECORD_AUDIO,
        ) == PackageManager.PERMISSION_GRANTED
        if (granted) {
            manager.startCall(pairing, number)
        } else {
            micPermission.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    // 键盘输入：+ 只允许作首字符、不占位数额度（与 Validation.NUMBER_RE 对齐），
    // 数字部分不超过 32
    fun appendKey(key: String) {
        message = null
        if (key == "+") {
            if (number.isEmpty()) number = "+"
            return
        }
        if (number.removePrefix("+").length < 32) number += key
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
            // 小屏/横屏/大字体下内容超高时可滚动，保证拨号键永远可达
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 24.dp),
    ) {
        Spacer(Modifier.height(16.dp))
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("CallPilot", style = MaterialTheme.typography.titleLarge)
            Spacer(Modifier.weight(1f))
            TextButton(
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
            ) {
                Text(
                    "解除配对",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
        }

        Spacer(Modifier.height(8.dp))
        Surface(
            shape = RoundedCornerShape(16.dp),
            color = MaterialTheme.colorScheme.surfaceVariant,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Row(
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 14.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                val enabled = status?.edgeEnabled == true
                Box(
                    Modifier
                        .size(10.dp)
                        .clip(CircleShape)
                        .background(
                            if (enabled) Brand.Green else MaterialTheme.colorScheme.outline,
                        ),
                )
                Spacer(Modifier.size(10.dp))
                Column {
                    Text(
                        when {
                            status == null -> "线路状态获取中…"
                            enabled -> "远程拨号已就绪"
                            else -> "远程拨号未启用"
                        },
                        style = MaterialTheme.typography.titleSmall,
                    )
                    Text(
                        "设备 ${pairing.displayName} · 经远端 SIM 外呼",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }

        Spacer(Modifier.height(40.dp))

        // 号码显示行：号码绝对居中，退格叠放在右缘（点删一位，长按清空）
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(56.dp),
            contentAlignment = Alignment.Center,
        ) {
            if (number.isEmpty()) {
                Text(
                    "输入号码",
                    fontSize = 30.sp,
                    color = MaterialTheme.colorScheme.outline,
                )
            } else {
                Text(
                    number,
                    fontSize = when {
                        number.length <= 12 -> 34.sp
                        number.length <= 18 -> 26.sp
                        else -> 20.sp
                    },
                    fontFamily = FontFamily.Monospace,
                    maxLines = 1,
                    modifier = Modifier.padding(horizontal = 44.dp),
                )
                Box(
                    modifier = Modifier
                        .align(Alignment.CenterEnd)
                        .size(44.dp)
                        .clip(CircleShape)
                        .combinedClickable(
                            onClick = { number = number.dropLast(1) },
                            onLongClick = { number = "" },
                        ),
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        "⌫",
                        fontSize = 22.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }

        message?.let {
            Spacer(Modifier.height(6.dp))
            Text(
                it,
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.fillMaxWidth(),
            )
        }

        Spacer(Modifier.height(18.dp))
        Keypad(
            onKey = ::appendKey,
            zeroLongPressPlus = true,
            modifier = Modifier.align(Alignment.CenterHorizontally),
        )

        Spacer(Modifier.height(22.dp))
        val canDial = Validation.isValidNumber(number)
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.Center,
        ) {
            Box(
                modifier = Modifier
                    .size(72.dp)
                    .clip(CircleShape)
                    .background(
                        if (canDial) Brand.Green
                        else MaterialTheme.colorScheme.surfaceVariant,
                    )
                    .combinedClickable(enabled = canDial, onClick = ::dial),
                contentAlignment = Alignment.Center,
            ) {
                Text("📞", fontSize = 28.sp, color = Color.White)
            }
        }
        Spacer(Modifier.height(28.dp))
    }
}
