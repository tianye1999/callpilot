package ai.bondings.callpilot.ui

import ai.bondings.callpilot.call.CallManager
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.DeviceStatus
import ai.bondings.callpilot.protocol.GatewayClient
import ai.bondings.callpilot.protocol.HostedCloudClient
import ai.bondings.callpilot.protocol.HostedDeviceStatus
import ai.bondings.callpilot.protocol.PairingProtocol
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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
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
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private const val LINE_STATUS_REFRESH_MS = 15_000L

internal fun isDialEnabled(number: String, lineReady: Boolean?): Boolean =
    Validation.isValidNumber(number) && lineReady == true

/** 拨号页 v3：状态卡片 + 大号显示 + 自绘 12 键键盘（免系统输入法）+ 绿色拨号键。 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun DialScreen(
    pairing: StoredPairing,
    manager: CallManager,
) {
    val context = LocalContext.current
    var lineReady by remember(pairing) { mutableStateOf<Boolean?>(null) }
    var lineStatusLabel by remember(pairing) { mutableStateOf("线路状态获取中…") }
    var lineStatusError by remember(pairing) { mutableStateOf<String?>(null) }
    var number by remember { mutableStateOf("") }
    var message by remember { mutableStateOf<String?>(null) }

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

    LaunchedEffect(pairing, context) {
        val tunnelClient = if (pairing.protocol == PairingProtocol.TUNNEL) {
            GatewayClient(pairing.gatewayUrl).also { it.credential = pairing.credential }
        } else {
            null
        }
        val hostedClient = if (pairing.protocol == PairingProtocol.HOSTED) {
            HostedCloudClient(pairing.gatewayUrl).also { it.credential = pairing.credential }
        } else {
            null
        }

        suspend fun refreshLineStatus() {
            try {
                val status = withContext(Dispatchers.IO) {
                    when (pairing.protocol) {
                        PairingProtocol.TUNNEL -> checkNotNull(tunnelClient).deviceStatus()
                        PairingProtocol.HOSTED -> checkNotNull(hostedClient).deviceStatus()
                    }
                }
                when (status) {
                    is DeviceStatus -> {
                        lineReady = status.edgeEnabled && status.modemOnline
                        lineStatusLabel = when {
                            !status.edgeEnabled -> "远程拨号未启用"
                            !status.modemOnline -> "SIM 线路离线"
                            else -> "远程拨号已就绪"
                        }
                    }
                    is HostedDeviceStatus -> {
                        lineReady = status.connected && status.modemOnline
                        lineStatusLabel = when {
                            !status.connected -> "电脑端离线"
                            !status.modemOnline -> "SIM 线路离线"
                            else -> "远程拨号已就绪"
                        }
                    }
                }
                lineStatusError = null
            } catch (e: Exception) {
                lineReady = false
                lineStatusLabel = "线路状态暂不可用"
                lineStatusError = "获取线路状态失败：${e.message}"
            }
        }

        val lifecycleOwner = context as? LifecycleOwner
        if (lifecycleOwner == null) {
            refreshLineStatus()
        } else {
            lifecycleOwner.lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
                while (true) {
                    refreshLineStatus()
                    delay(LINE_STATUS_REFRESH_MS)
                }
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
                val enabled = lineReady == true
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
                        lineStatusLabel,
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

        (message ?: lineStatusError)?.let {
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
        val canDial = isDialEnabled(number, lineReady)
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
