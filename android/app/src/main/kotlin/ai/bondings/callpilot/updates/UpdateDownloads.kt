package ai.bondings.callpilot.updates

import ai.bondings.callpilot.BuildConfig
import ai.bondings.callpilot.R
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Environment
import android.util.Log
import java.io.File
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

internal data class UpdateDownload(val id: Long, val version: String, val file: File)

internal enum class DownloadState { MISSING, RUNNING, READY, FAILED }

internal class UpdateDownloads private constructor(private val context: Context) {
    private val prefs = context.getSharedPreferences("update_download", Context.MODE_PRIVATE)
    private val downloads = context.getSystemService(DownloadManager::class.java)
    private val mutex = Mutex()
    private val mutableChanges = MutableStateFlow(0L)
    val changes = mutableChanges.asStateFlow()
    var visible = false

    private fun saved(): UpdateDownload? {
        val id = prefs.getLong("id", -1)
        val version = prefs.getString("version", null) ?: return null
        val name = prefs.getString("file", null) ?: return null
        if (id < 0 || !UpdatePolicy.isNewer(version, BuildConfig.VERSION_NAME) ||
            !Regex("[a-f0-9-]{36}\\.apk").matches(name)
        ) return null
        val directory = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: return null
        return UpdateDownload(id, version, File(directory, "updates/$name"))
    }

    suspend fun startOrResume(release: AppRelease?): UpdateDownload? = withContext(Dispatchers.IO) {
        mutex.withLock {
            val previous = saved()
            if (previous != null && (release == null || previous.version == release.version) &&
                status(previous) in setOf(DownloadState.RUNNING, DownloadState.READY)
            ) return@withLock previous
            if (release == null || !UpdatePolicy.isNewer(release.version, BuildConfig.VERSION_NAME) ||
                !UpdatePolicy.validDownloadUrl(release.downloadUrl)
            ) return@withLock null
            if (previous != null) downloads.remove(previous.id)
            prefs.edit().clear().commit()
            val directory = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
                ?: error("External app storage unavailable")
            val name = "${UUID.randomUUID()}.apk"
            val request = DownloadManager.Request(Uri.parse(release.downloadUrl))
                .setTitle(context.getString(R.string.update_available, release.version))
                .setMimeType(APK_MIME)
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE)
                .setDestinationInExternalFilesDir(context, Environment.DIRECTORY_DOWNLOADS, "updates/$name")
            val id = downloads.enqueue(request)
            if (!prefs.edit().putLong("id", id).putString("version", release.version)
                    .putString("file", name).commit()
            ) {
                downloads.remove(id)
                error("Cannot remember update download")
            }
            UpdateDownload(id, release.version, File(directory, "updates/$name"))
        }
    }

    suspend fun status(download: UpdateDownload): DownloadState = withContext(Dispatchers.IO) {
        downloads.query(DownloadManager.Query().setFilterById(download.id))?.use { cursor ->
            if (!cursor.moveToFirst()) return@withContext DownloadState.MISSING
            when (cursor.getInt(cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))) {
                DownloadManager.STATUS_SUCCESSFUL -> {
                    val source = cursor.getString(cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_URI))
                    val local = cursor.getString(cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_LOCAL_URI))
                    if (UpdatePolicy.validDownloadUrl(source) && local != null &&
                        Uri.parse(local) == Uri.fromFile(download.file) && download.file.isFile
                    ) DownloadState.READY else DownloadState.FAILED
                }
                DownloadManager.STATUS_FAILED -> DownloadState.FAILED
                else -> DownloadState.RUNNING
            }
        } ?: DownloadState.MISSING
    }

    suspend fun completed(id: Long) {
        val download = withContext(Dispatchers.IO) { saved() } ?: return
        if (download.id != id) return
        val state = status(download)
        mutableChanges.value++
        if (state == DownloadState.READY && !visible) {
            AppUpdates.get(context).notify(
                context.getString(R.string.update_install, download.version),
                context.getString(R.string.update_ready),
            )
        } else if (state == DownloadState.FAILED || state == DownloadState.MISSING) {
            Log.w(AppUpdates.TAG, "Update download failed")
        }
    }

    companion object {
        const val APK_MIME = "application/vnd.android.package-archive"
        @SuppressLint("StaticFieldLeak")
        private var instance: UpdateDownloads? = null

        fun get(context: Context): UpdateDownloads = instance ?: UpdateDownloads(context.applicationContext).also { instance = it }
    }
}
