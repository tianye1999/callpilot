package ai.bondings.callpilot.updates

import ai.bondings.callpilot.R
import ai.bondings.callpilot.ui.CallPilotTheme
import android.content.ClipData
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Non-exported notification target; no URL or filesystem path is accepted from an Intent. */
class UpdateActivity : ComponentActivity() {
    private val downloads by lazy { UpdateDownloads.get(this) }
    private var permissionRequested = false
    private var permissionInFlight = false
    private var installing = false
    private val permission = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        permissionInFlight = false
        if (!packageManager.canRequestPackageInstalls()) finish()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        permissionRequested = savedInstanceState?.getBoolean("permission_requested") ?: false
        permissionInFlight = savedInstanceState?.getBoolean("permission_in_flight") ?: false
        setContent {
            CallPilotTheme {
                Surface(Modifier.fillMaxSize()) {
                    Column(
                        Modifier.fillMaxSize().safeDrawingPadding().padding(24.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(20.dp, Alignment.CenterVertically),
                    ) {
                        CircularProgressIndicator()
                        Text(stringResource(R.string.update_downloading))
                        TextButton(onClick = { finish() }) { Text(stringResource(R.string.common_back)) }
                    }
                }
            }
        }
        lifecycleScope.launch {
            try {
                val download = downloads.startOrResume(AppUpdates.get(this@UpdateActivity).latest.value)
                if (download == null) {
                    finish()
                    return@launch
                }
                repeatOnLifecycle(Lifecycle.State.RESUMED) {
                    downloads.changes.collect {
                        if (installing || permissionInFlight || isFinishing) return@collect
                        when (downloads.status(download)) {
                            DownloadState.READY -> install(download)
                            DownloadState.RUNNING -> Unit
                            else -> {
                                Log.w(AppUpdates.TAG, "Update download unavailable")
                                finish()
                            }
                        }
                    }
                }
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                Log.w(AppUpdates.TAG, "Update installation failed: ${error.javaClass.simpleName}")
                finish()
            }
        }
    }

    private suspend fun install(download: UpdateDownload) {
        // Signature enforcement applies to upgrades of this package, not unrelated APKs.
        val archive = withContext(Dispatchers.IO) {
            packageManager.getPackageArchiveInfo(download.file.absolutePath, 0)
        }
        require(archive?.packageName == packageName) { "Not an update for this app" }
        if (!packageManager.canRequestPackageInstalls()) {
            if (permissionRequested) {
                finish()
                return
            }
            permissionRequested = true
            permissionInFlight = true
            permission.launch(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
            return
        }
        val uri = FileProvider.getUriForFile(this, "$packageName.updates", download.file)
        val intent = Intent(Intent.ACTION_VIEW).setDataAndType(uri, UpdateDownloads.APK_MIME)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        intent.clipData = ClipData.newRawUri("update", uri)
        installing = true
        startActivity(intent)
        AppUpdates.get(this).clearNotification()
        finish()
    }

    override fun onResume() {
        super.onResume()
        downloads.visible = true
    }

    override fun onPause() {
        downloads.visible = false
        super.onPause()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        outState.putBoolean("permission_requested", permissionRequested)
        outState.putBoolean("permission_in_flight", permissionInFlight)
        super.onSaveInstanceState(outState)
    }
}
