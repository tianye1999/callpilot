package ai.bondings.callpilot.ui

import ai.bondings.callpilot.R
import ai.bondings.callpilot.call.OfferAlerts
import ai.bondings.callpilot.call.OfferWatchService
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Card
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner

@Composable
fun OfferWatchSettingsCard(hostedPairing: Boolean) {
    val context = LocalContext.current
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val power = remember { context.getSystemService(PowerManager::class.java) }
    val notifications = remember { context.getSystemService(NotificationManager::class.java) }
    val running by OfferWatchService.running.collectAsState()
    var exempt by remember { mutableStateOf(power.isIgnoringBatteryOptimizations(context.packageName)) }
    var fullScreen by remember { mutableStateOf(OfferAlerts.canUseFullScreenIntent(context)) }
    var notificationsEnabled by remember { mutableStateOf(notifications.areNotificationsEnabled()) }

    DisposableEffect(lifecycle) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) {
                exempt = power.isIgnoringBatteryOptimizations(context.packageName)
                fullScreen = OfferAlerts.canUseFullScreenIntent(context)
                notificationsEnabled = notifications.areNotificationsEnabled()
            }
        }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer) }
    }

    Card(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 12.dp),
        shape = RoundedCornerShape(8.dp),
    ) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(stringResource(R.string.offer_settings_title), style = MaterialTheme.typography.titleMedium)
            Text(stringResource(if (running) R.string.offer_service_running else R.string.offer_service_stopped))
            Text(stringResource(if (exempt) R.string.offer_battery_exempt else R.string.offer_battery_restricted))
            if (!hostedPairing) Text(stringResource(R.string.offer_hosted_only))
            if (!exempt && hostedPairing) {
                SettingsAction(stringResource(R.string.offer_battery_action)) {
                    openOfferSetting(context, Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS))
                }
            }
            if (Build.VERSION.SDK_INT >= 34 && !fullScreen && hostedPairing) {
                SettingsAction(stringResource(R.string.offer_full_screen_action)) {
                    openOfferSetting(context, Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT))
                }
            }
            if (!notificationsEnabled) {
                Text(stringResource(R.string.offer_notifications_disabled), color = MaterialTheme.colorScheme.error)
            }
            SettingsAction(stringResource(R.string.offer_notification_settings)) {
                openOfferSetting(
                    context,
                    Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName),
                )
            }
            Text(stringResource(R.string.offer_autostart_hint), style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun SettingsAction(label: String, onClick: () -> Unit) {
    OutlinedButton(onClick = onClick, modifier = Modifier.heightIn(min = 48.dp)) {
        Icon(Icons.Filled.Settings, contentDescription = null)
        Text(label, Modifier.padding(start = 8.dp))
    }
}

private fun openOfferSetting(context: Context, intent: Intent) {
    val packageUri = Uri.parse("package:${context.packageName}")
    if (intent.action != Settings.ACTION_APP_NOTIFICATION_SETTINGS) intent.data = packageUri
    runCatching { context.startActivity(intent) }.onFailure {
        // 部分国产 ROM 没有标准设置页，退回应用详情，不因跳转失败崩溃。
        runCatching { context.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, packageUri)) }
    }
}
