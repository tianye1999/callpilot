package ai.bondings.callpilot.call

import ai.bondings.callpilot.MainActivity
import ai.bondings.callpilot.R
import ai.bondings.callpilot.content.ProtectedJsonStore
import ai.bondings.callpilot.pairing.CredentialStore
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.HostedCloudClient
import ai.bondings.callpilot.protocol.HostedCloudException
import ai.bondings.callpilot.protocol.InboundOffer
import ai.bondings.callpilot.protocol.InboundOfferState
import ai.bondings.callpilot.protocol.CallSummaryState
import ai.bondings.callpilot.protocol.HttpClients
import ai.bondings.callpilot.protocol.PairingProtocol
import ai.bondings.callpilot.updates.AppUpdates
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import java.io.IOException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** 等待转接、通话摘要与短信提醒；麦克风与接听仍由 CallManager / CallService 管理。 */
class OfferWatchService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var polling: Job? = null
    private val ownerWatches = mutableMapOf<String, OwnerWatch>()
    private var ownerWatchPausedUntil = 0L

    private class OwnerWatch(val pairing: StoredPairing, val job: Job)
    private var ready = false
    private var wakeLock: PowerManager.WakeLock? = null
    private var renewWakeLockAt = 0L

    override fun onCreate() {
        super.onCreate()
        val notifications = getSystemService(NotificationManager::class.java)
        notifications.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, getString(R.string.offer_watch_channel), NotificationManager.IMPORTANCE_LOW).apply {
                setSound(null, null)
                enableVibration(false)
            },
        )
        val pending = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_callpilot_notification)
            .setContentTitle(getString(R.string.offer_watch_notification))
            .setContentIntent(pending)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
        try {
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
            ready = true
        } catch (_: SecurityException) {
            stopSelf()
        } catch (_: IllegalStateException) {
            stopSelf()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!ready) return START_NOT_STICKY
        if (polling?.isActive == true) return START_STICKY
        val store = runCatching { CredentialStore(applicationContext) }.getOrNull()
        val paired = runCatching { store?.load() }.getOrNull()
        if (store == null || paired?.protocol != PairingProtocol.HOSTED) {
            stopSelf()
            return START_NOT_STICKY
        }
        val alerts = OfferAlerts.get(this)
        DirectCallCapabilities.cache.activate(paired)
        alerts.bindPairing(paired.credential.deviceId)
        val manager = CallGraph.manager(this)
        mutableRunning.value = true
        scope.launch {
            CredentialStore.changes.collect {
                val current = runCatching { store.load() }.getOrNull()
                DirectCallCapabilities.cache.activate(current)
                if (current?.protocol != PairingProtocol.HOSTED) {
                    alerts.reset()
                    stopWatching()
                } else {
                    // 换了另一份云配对：旧配对的长轮询立刻取消（连带底层 HTTP），能力退避也重算。
                    cancelOwnerWatches { it.pairing != current }
                    ownerWatchPausedUntil = 0L
                }
            }
        }
        scope.launch {
            manager.state.collect { state ->
                alerts.tick(state !is CallState.Idle)
                updateWakeLock(state is CallState.Idle)
            }
        }
        scope.launch {
            while (isActive) {
                val idle = manager.state.value is CallState.Idle
                alerts.tick(!idle)
                updateWakeLock(idle)
                // 拒绝 / 过期 / 进入通话都不等下一次列表轮询：立刻撤掉对应长轮询。
                if (!idle) cancelOwnerWatches() else pruneOwnerWatches(alerts)
                delay(1_000)
            }
        }
        polling = scope.launch {
            while (isActive) {
                val started = SystemClock.elapsedRealtime()
                try {
                    val current = store.load()
                    if (current?.protocol != PairingProtocol.HOSTED) {
                        alerts.reset()
                        stopWatching()
                        return@launch
                    }
                    alerts.bindPairing(current.credential.deviceId)
                    if (manager.state.value !is CallState.Idle) cancelOwnerWatches()
                    if (manager.state.value is CallState.Idle) {
                        val client = HostedCloudClient(current.gatewayUrl, HttpClients.shared).also {
                            it.credential = current.credential
                        }
                        try {
                            val offers = withContext(Dispatchers.IO) { client.listInboundOffers() }
                            if (store.load() == current && manager.state.value is CallState.Idle) {
                                alerts.update(offers)
                                syncOwnerWatches(current, alerts, store)
                            }
                        } catch (error: HostedCloudException) {
                            if (error.statusCode == 401 && revokeIfCurrent(store, current, alerts)) return@launch
                        }
                    }
                } catch (error: CancellationException) {
                    throw error
                } catch (_: IOException) {
                    HttpClients.dropPooledConnections()
                } catch (_: Exception) {
                    // 离线、响应异常均静默重试，不能结束等待循环。
                }
                delay((POLL_INTERVAL_MS - (SystemClock.elapsedRealtime() - started)).coerceAtLeast(1))
            }
        }
        startDigestPolling(store, manager)
        startSmsPolling(store)
        scope.launch {
            val updates = AppUpdates.get(this@OfferWatchService)
            while (isActive) {
                updates.check()
                delay(updates.nextCheckDelay())
            }
        }
        return START_STICKY
    }

    private fun startDigestPolling(store: CredentialStore, manager: CallManager) {
        val digests = CallDigestStore(applicationContext)
        val alerts = CallDigestAlerts(applicationContext)
        // 与 3 秒 offer 轮询独立；详情接口慢或退避都不占用转接循环。
        scope.launch {
            while (isActive) {
                val started = SystemClock.elapsedRealtime()
                var interval = DIGEST_POLL_INTERVAL_MS
                try {
                    val current = store.load()
                    if (current?.protocol != PairingProtocol.HOSTED) {
                        stopWatching()
                        return@launch
                    }
                    val policy = digests.load(current.credential.deviceId, System.currentTimeMillis())
                    fun canNotify() = store.load() == current && manager.state.value is CallState.Idle
                    if (canNotify()) {
                        val client = HostedCloudClient(current.gatewayUrl, HttpClients.shared).also {
                            it.credential = current.credential
                        }
                        try {
                            val page = client.listCallRecords(limit = 20, cursor = null)
                            if (canNotify()) {
                                for (record in policy.pending(page.items, System.currentTimeMillis())) {
                                    if (!canNotify()) break
                                    val detail = if (!CallDigestPolicy.isMissed(record) && record.summaryState != CallSummaryState.PENDING) {
                                        try {
                                            client.getCallRecord(record.callId).takeIf { it.record.callId == record.callId }
                                        } catch (error: CancellationException) {
                                            throw error
                                        } catch (error: HostedCloudException) {
                                            if (error.statusCode == 401 || error.statusCode == 403 || error.errorCode == "FEATURE_DISABLED") throw error
                                            null
                                        } catch (_: Exception) {
                                            null
                                        }
                                    } else null
                                    if (!canNotify()) break
                                    // 详情可能比列表更新：已由手机接听或重新变为 PENDING 时也不误报。
                                    val latest = detail?.record?.let {
                                        it.copy(summaryPreview = it.summaryPreview?.takeIf(String::isNotBlank) ?: record.summaryPreview)
                                    } ?: record
                                    if (policy.pending(listOf(latest), System.currentTimeMillis()).isEmpty()) continue
                                    if (alerts.show(latest, detail?.summary)) {
                                        policy.notified(latest.callId)
                                        digests.save(policy)
                                    }
                                }
                            }
                        } catch (error: HostedCloudException) {
                            if (error.statusCode == 401 && store.load() == current) {
                                // 与 offer 共用撤销流程，旧请求不能清掉新的配对。
                                ProtectedJsonStore.clearAll(applicationContext)
                                store.clear()
                                OfferAlerts.get(this@OfferWatchService).reset()
                                stopWatching()
                                return@launch
                            }
                            if (error.statusCode == 403 || error.errorCode == "FEATURE_DISABLED") {
                                interval = DIGEST_BACKOFF_MS
                            }
                        }
                    }
                } catch (error: CancellationException) {
                    throw error
                } catch (_: Exception) {
                    // 摘要接口异常静默重试，不清理共享连接池以免干扰 offer。
                }
                delay((interval - (SystemClock.elapsedRealtime() - started)).coerceAtLeast(1))
            }
        }
    }

    private fun startSmsPolling(store: CredentialStore) {
        val messages = SmsAlertStore(applicationContext)
        val alerts = SmsAlerts(applicationContext)
        // 短信与转接、摘要各自轮询；本机通话期间也继续查询。
        scope.launch {
            while (isActive) {
                val started = SystemClock.elapsedRealtime()
                var interval = SMS_POLL_INTERVAL_MS
                try {
                    val current = store.load()
                    if (current?.protocol != PairingProtocol.HOSTED) {
                        stopWatching()
                        return@launch
                    }
                    val policy = messages.load(current.credential.deviceId, System.currentTimeMillis())
                    val client = HostedCloudClient(current.gatewayUrl, HttpClients.shared).also {
                        it.credential = current.credential
                    }
                    try {
                        // 接口按 occurredAt 倒序，null 游标取最新页；提醒资格按 recordedAt 判断。
                        val page = client.listMessages(limit = 20, cursor = null)
                        if (store.load() == current) {
                            for (action in policy.pending(page.items, System.currentTimeMillis())) {
                                if (store.load() != current) break
                                if (alerts.show(action.message, action.silent)) {
                                    policy.notified(action.message)
                                    messages.save(policy)
                                }
                            }
                        }
                    } catch (error: HostedCloudException) {
                        if (error.statusCode == 401 && store.load() == current) {
                            // 沿用撤销流程，旧请求的 401 不能清掉新配对。
                            ProtectedJsonStore.clearAll(applicationContext)
                            store.clear()
                            OfferAlerts.get(this@OfferWatchService).reset()
                            stopWatching()
                            return@launch
                        }
                        if (error.statusCode == 403 || error.errorCode == "FEATURE_DISABLED") {
                            interval = SMS_BACKOFF_MS
                        }
                    }
                } catch (error: CancellationException) {
                    throw error
                } catch (_: Exception) {
                    // 网络与响应异常静默重试，不清理共享连接池以免干扰转接。
                }
                delay((interval - (SystemClock.elapsedRealtime() - started)).coerceAtLeast(1))
            }
        }
    }

    /**
     * 对每个仍在响铃的 offer 向中转长轮询终态：他机接听、机主拒绝或来电结束时立刻收铃，
     * 不等 3 s 轮询。中转或 Edge 未开此能力（404 FEATURE_DISABLED）时退回轮询，稍后再试。
     */
    private fun syncOwnerWatches(current: StoredPairing, alerts: OfferAlerts, store: CredentialStore) {
        val wanted = alerts.pendingWatch(System.currentTimeMillis()).associateBy { it.offerId }
        cancelOwnerWatches { watch -> watch.pairing != current || !watch.job.isActive }
        val iterator = ownerWatches.entries.iterator()
        while (iterator.hasNext()) {
            val entry = iterator.next()
            if (entry.key in wanted) continue
            entry.value.job.cancel()
            iterator.remove()
        }
        if (SystemClock.elapsedRealtime() >= ownerWatchPausedUntil) {
            for ((offerId, offer) in wanted) {
                if (offerId in ownerWatches) continue
                ownerWatches[offerId] = OwnerWatch(current, scope.launch { watchOwnerState(current, offer, alerts, store) })
            }
        }
        if (wanted.isEmpty()) return
        val direct = DirectCallCapabilities.cache.cached(current).inboundFastpathV2
        DirectCallCapabilities.cache.refresh(current)
        if (store.load() != current || CallGraph.manager(this).state.value !is CallState.Idle) return
        val pending = alerts.pendingWatch(System.currentTimeMillis())
        if (direct) {
            pending.firstOrNull()?.let { CallGraph.manager(this).prepareTakeover(current, it) }
        } else {
            wanted.values.forEach { CallGraph.manager(this).discardPrepared(it.offerId) }
        }
    }

    private fun cancelOwnerWatches(predicate: (OwnerWatch) -> Boolean = { true }) {
        val iterator = ownerWatches.entries.iterator()
        while (iterator.hasNext()) {
            val entry = iterator.next()
            if (!predicate(entry.value)) continue
            if (entry.value.job.isActive) entry.value.job.cancel()
            iterator.remove()
        }
    }

    /** 被拒绝 / 已过期的 offer 不再值得盯：取消其长轮询（取消会一并取消底层 HTTP）。 */
    private fun pruneOwnerWatches(alerts: OfferAlerts) {
        val keep = alerts.pendingWatch(System.currentTimeMillis()).map { it.offerId }.toSet()
        val iterator = ownerWatches.entries.iterator()
        while (iterator.hasNext()) {
            val entry = iterator.next()
            if (entry.key in keep && entry.value.job.isActive) continue
            if (entry.value.job.isActive) entry.value.job.cancel()
            iterator.remove()
        }
    }

    /** 旧请求的 401 不得清掉用户刚完成的新配对；只有仍是当前配对时才撤销。 */
    private fun revokeIfCurrent(store: CredentialStore, current: StoredPairing, alerts: OfferAlerts): Boolean {
        if (store.load() != current) return false
        ProtectedJsonStore.clearAll(applicationContext)
        store.clear()
        alerts.reset()
        stopWatching()
        return true
    }

    private suspend fun watchOwnerState(
        current: StoredPairing,
        offer: InboundOffer,
        alerts: OfferAlerts,
        store: CredentialStore,
    ) {
        val client = HostedCloudClient(current.gatewayUrl).also { it.credential = current.credential }
        while (offer.expiresAt > System.currentTimeMillis() && !alerts.isDismissed(offer.offerId)) {
            // 配对换了或已进入通话：这条长轮询没有意义，直接退出。
            if (store.load() != current || CallGraph.manager(this).state.value !is CallState.Idle) return
            val state = try {
                withContext(Dispatchers.IO) { client.watchInboundOfferCancellable(offer.offerId) }
            } catch (error: CancellationException) {
                throw error
            } catch (error: HostedCloudException) {
                if (error.statusCode == 404 || error.errorCode == "FEATURE_DISABLED") {
                    ownerWatchPausedUntil = SystemClock.elapsedRealtime() + OWNER_WATCH_PAUSE_MS
                    return
                }
                if (error.statusCode == 401) {
                    revokeIfCurrent(store, current, alerts)
                    return
                }
                delay(OWNER_WATCH_RETRY_MS)
                continue
            } catch (_: Exception) {
                delay(OWNER_WATCH_RETRY_MS)
                continue
            }
            if (state == InboundOfferState.OFFERED) continue
            if (store.load() == current) alerts.resolve(offer.offerId, state)
            if (state != InboundOfferState.CLAIMED) CallGraph.manager(this).discardPrepared(offer.offerId)
            return
        }
    }

    private fun updateWakeLock(waiting: Boolean) {
        if (!waiting) {
            releaseWakeLock()
            return
        }
        val now = SystemClock.elapsedRealtime()
        if (wakeLock?.isHeld == true && now < renewWakeLockAt) return
        val lock = wakeLock ?: getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "CallPilot:OfferWatch")
            .apply { setReferenceCounted(false) }.also { wakeLock = it }
        // 3 秒轮询需 CPU 保持运行；两分钟兜底超时，服务活着才续期。
        lock.acquire(120_000)
        renewWakeLockAt = now + 60_000
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        renewWakeLockAt = 0
    }

    private fun stopWatching() {
        cancelOwnerWatches()
        scope.cancel()
        releaseWakeLock()
        mutableRunning.value = false
        OfferAlerts.get(this).clearNotifications()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        stopWatching()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val CHANNEL_ID = "callpilot_offer_watch"
        private const val NOTIFICATION_ID = 1002
        private const val POLL_INTERVAL_MS = 3_000L
        private const val OWNER_WATCH_RETRY_MS = 1_000L
        private const val OWNER_WATCH_PAUSE_MS = 300_000L
        private const val DIGEST_POLL_INTERVAL_MS = 30_000L
        private const val DIGEST_BACKOFF_MS = 300_000L
        private const val SMS_POLL_INTERVAL_MS = 15_000L
        private const val SMS_BACKOFF_MS = 300_000L
        private val mutableRunning = MutableStateFlow(false)
        val running = mutableRunning.asStateFlow()

        /** 仅由前台 Activity 或 BOOT_COMPLETED 调用，不尝试绕过系统启动限制。 */
        fun startIfPaired(context: Context) {
            val paired = runCatching { CredentialStore(context.applicationContext).load() }.getOrNull()
            if (paired?.protocol != PairingProtocol.HOSTED) {
                stop(context)
                return
            }
            try {
                context.startForegroundService(Intent(context, OfferWatchService::class.java))
            } catch (_: IllegalStateException) {
                // 厂商拒绝后台启动时，下一次打开 App 再尝试。
            } catch (_: SecurityException) {
                // 系统权限或策略限制，不影响用户正常打开 App。
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, OfferWatchService::class.java))
            OfferAlerts.get(context).clearNotifications()
        }
    }
}
