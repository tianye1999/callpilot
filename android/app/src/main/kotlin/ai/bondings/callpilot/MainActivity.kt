package ai.bondings.callpilot

import ai.bondings.callpilot.call.CallGraph
import ai.bondings.callpilot.call.CallDigestAlerts
import ai.bondings.callpilot.call.CallState
import ai.bondings.callpilot.call.OfferAlerts
import ai.bondings.callpilot.call.OfferWatchService
import ai.bondings.callpilot.call.SmsAlerts
import ai.bondings.callpilot.content.ProtectedJsonStore
import ai.bondings.callpilot.pairing.CredentialStore
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.HostedCloudClient
import ai.bondings.callpilot.protocol.HostedCloudException
import ai.bondings.callpilot.protocol.ContentWireValidation
import ai.bondings.callpilot.protocol.InboundOffer
import ai.bondings.callpilot.protocol.PairingProtocol
import ai.bondings.callpilot.ui.CallPilotTheme
import ai.bondings.callpilot.ui.CallScreen
import ai.bondings.callpilot.ui.IncomingOfferScreen
import ai.bondings.callpilot.ui.MainTabShell
import ai.bondings.callpilot.ui.PairScreen
import ai.bondings.callpilot.ui.RootPresentation
import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.Alignment
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.semantics.hideFromAccessibility
import androidx.compose.ui.semantics.semantics
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

private const val OFFER_POLL_INTERVAL_MS = 3_000L

/** Root host: the paired tab shell remains mounted while call/offer UI overlays it. */
class MainActivity : ComponentActivity() {
    private var offerRefreshVersion by mutableIntStateOf(0)
    private var showingOverLockscreen by mutableStateOf(false)
    private var offerLookupPending by mutableStateOf(false)
    private var digestCallId by mutableStateOf<String?>(null)
    private var smsMessageId by mutableStateOf<String?>(null)

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleOfferIntent(intent)
        handleDigestIntent(intent)
        handleSmsIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        OfferAlerts.get(this).setForeground(true)
    }

    override fun onPause() {
        OfferAlerts.get(this).setForeground(false)
        super.onPause()
    }

    private fun handleOfferIntent(intent: Intent) {
        val fromOffer = intent.action == OfferAlerts.ACTION_INCOMING_OFFER &&
            intent.getLongExtra(OfferAlerts.EXTRA_EXPIRES_AT, 0) > System.currentTimeMillis() &&
            CredentialStore(applicationContext).load()?.protocol == PairingProtocol.HOSTED
        offerLookupPending = fromOffer
        setOfferLockscreen(fromOffer)
        if (fromOffer) offerRefreshVersion++
    }

    private fun handleDigestIntent(intent: Intent) {
        val callId = intent.getStringExtra(CallDigestAlerts.EXTRA_CALL_ID)
        digestCallId = callId?.takeIf {
            intent.action == CallDigestAlerts.ACTION_CALL_DIGEST && ContentWireValidation.validCallId(it) &&
                CredentialStore(applicationContext).load()?.protocol == PairingProtocol.HOSTED
        }
        if (digestCallId == null) intent.removeExtra(CallDigestAlerts.EXTRA_CALL_ID)
    }

    private fun handleSmsIntent(intent: Intent) {
        val messageId = intent.getStringExtra(SmsAlerts.EXTRA_MESSAGE_ID)
        smsMessageId = messageId?.takeIf {
            intent.action == SmsAlerts.ACTION_SMS && ContentWireValidation.validMessageId(it) &&
                CredentialStore(applicationContext).load()?.protocol == PairingProtocol.HOSTED
        }
        if (smsMessageId == null) intent.removeExtra(SmsAlerts.EXTRA_MESSAGE_ID)
    }

    private fun setOfferLockscreen(enabled: Boolean) {
        showingOverLockscreen = enabled
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(enabled)
            setTurnScreenOn(enabled)
        } else {
            @Suppress("DEPRECATION")
            val flags = WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
            if (enabled) window.addFlags(flags) else window.clearFlags(flags)
        }
        if (!enabled) {
            intent.removeExtra(OfferAlerts.EXTRA_EXPIRES_AT)
            offerLookupPending = false
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handleOfferIntent(intent)
        handleDigestIntent(intent)
        handleSmsIntent(intent)
        enableEdgeToEdge()
        val store = CredentialStore(applicationContext)
        val manager = CallGraph.manager(applicationContext)
        val alerts = OfferAlerts.get(this)
        val permissionPrefs = getSharedPreferences("offer_permissions", MODE_PRIVATE)
        setContent {
            CallPilotTheme {
                Surface(
                    modifier = Modifier
                        .fillMaxSize()
                        .safeDrawingPadding(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    var pairing by remember { mutableStateOf<StoredPairing?>(store.load()) }
                    val callState by manager.state.collectAsState()
                    var incomingOffer by remember { mutableStateOf<InboundOffer?>(null) }
                    val current = pairing
                    val notificationPermission = rememberLauncherForActivityResult(
                        ActivityResultContracts.RequestPermission(),
                    ) { /* 拒绝后不反复打扰，设置页保留手动开启入口。 */ }

                    LaunchedEffect(Unit) {
                        CredentialStore.changes.collect {
                            pairing = store.load()
                        }
                    }

                    // 前台负责接听界面，服务只负责提醒；通知重入也立即刷新，不等轮询间隔。
                    LaunchedEffect(current, offerRefreshVersion) {
                        incomingOffer = null
                        if (current != null && current.protocol == PairingProtocol.HOSTED) {
                            alerts.bindPairing(current.credential.deviceId)
                            val offerClient = HostedCloudClient(current.gatewayUrl).also {
                                it.credential = current.credential
                            }
                            lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
                                OfferWatchService.startIfPaired(this@MainActivity)
                                if (Build.VERSION.SDK_INT >= 33 &&
                                    !permissionPrefs.getBoolean("notifications_requested", false) &&
                                    !showingOverLockscreen &&
                                    ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.POST_NOTIFICATIONS) !=
                                    PackageManager.PERMISSION_GRANTED
                                ) {
                                    permissionPrefs.edit().putBoolean("notifications_requested", true).apply()
                                    notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
                                }
                                while (true) {
                                    if (manager.state.value is CallState.Idle) {
                                        val available = try {
                                            withContext(Dispatchers.IO) {
                                                offerClient.listInboundOffers()
                                            }
                                        } catch (error: CancellationException) {
                                            throw error
                                        } catch (error: HostedCloudException) {
                                            if (error.statusCode == 401 && store.load() == current) {
                                                ProtectedJsonStore.clearAll(applicationContext)
                                                store.clear()
                                                OfferWatchService.stop(this@MainActivity)
                                            }
                                            emptyList()
                                        } catch (_: Exception) {
                                            emptyList()
                                        }
                                        val valid = available.filter {
                                            !alerts.isDismissed(it.offerId) &&
                                                it.expiresAt > System.currentTimeMillis()
                                        }
                                        val offer = valid.firstOrNull {
                                            it.offerId == intent.getStringExtra(OfferAlerts.EXTRA_OFFER_ID)
                                        } ?: valid.firstOrNull()
                                        if (store.load() == current && manager.state.value is CallState.Idle) {
                                            offer?.let { alerts.presented(it) }
                                            incomingOffer = offer
                                        }
                                    } else {
                                        incomingOffer = null
                                    }
                                    offerLookupPending = false
                                    if (incomingOffer == null && manager.state.value is CallState.Idle) {
                                        setOfferLockscreen(false)
                                    }
                                    delay(OFFER_POLL_INTERVAL_MS)
                                }
                            }
                        } else {
                            incomingOffer = null
                            setOfferLockscreen(false)
                            OfferWatchService.stop(this@MainActivity)
                        }
                    }

                    val activeOffer = incomingOffer
                    LaunchedEffect(activeOffer) {
                        if (activeOffer != null) {
                            delay((activeOffer.expiresAt - System.currentTimeMillis()).coerceAtLeast(0))
                            if (incomingOffer == activeOffer) incomingOffer = null
                            if (manager.state.value is CallState.Idle) setOfferLockscreen(false)
                        }
                    }
                    LaunchedEffect(callState) {
                        if (callState !is CallState.Idle) incomingOffer = null
                        else if (incomingOffer == null && !offerLookupPending) setOfferLockscreen(false)
                    }
                    var pendingMicrophoneOffer by remember { mutableStateOf<InboundOffer?>(null) }
                    val micPermission = rememberLauncherForActivityResult(
                        ActivityResultContracts.RequestPermission(),
                    ) { granted ->
                        val offer = pendingMicrophoneOffer
                        pendingMicrophoneOffer = null
                        val paired = pairing
                        if (granted && offer != null && paired != null && store.load() == paired &&
                            offer.expiresAt > System.currentTimeMillis() &&
                            incomingOffer?.offerId == offer.offerId && !alerts.isDismissed(offer.offerId) &&
                            manager.state.value is CallState.Idle
                        ) {
                            alerts.dismiss(offer)
                            incomingOffer = null
                            manager.answerTakeover(paired, offer.offerId)
                        }
                    }

                    val presentation = RootPresentation.resolve(
                        isPaired = current != null,
                        callState = callState,
                        incomingOffer = activeOffer,
                    )
                    if (presentation == RootPresentation.Pairing) {
                        PairScreen(store = store, onPaired = { pairing = it })
                    } else if (current != null) {
                        val overlayVisible = presentation != RootPresentation.Main || showingOverLockscreen
                        Box(Modifier.fillMaxSize()) {
                            MainTabShell(
                                pairing = current,
                                store = store,
                                manager = manager,
                                digestCallId = digestCallId,
                                onDigestOpened = {
                                    digestCallId = null
                                    intent.removeExtra(CallDigestAlerts.EXTRA_CALL_ID)
                                },
                                smsMessageId = smsMessageId,
                                onSmsOpened = {
                                    smsMessageId = null
                                    intent.removeExtra(SmsAlerts.EXTRA_MESSAGE_ID)
                                },
                                onUnpaired = {
                                    digestCallId = null
                                    intent.removeExtra(CallDigestAlerts.EXTRA_CALL_ID)
                                    smsMessageId = null
                                    intent.removeExtra(SmsAlerts.EXTRA_MESSAGE_ID)
                                    incomingOffer = null
                                    pairing = null
                                    alerts.reset()
                                    OfferWatchService.stop(this@MainActivity)
                                    setOfferLockscreen(false)
                                },
                                modifier = Modifier
                                    .fillMaxSize()
                                    .then(
                                        if (overlayVisible) {
                                            Modifier
                                                .pointerInput(Unit) {
                                                    awaitPointerEventScope {
                                                        while (true) {
                                                            awaitPointerEvent().changes.forEach { it.consume() }
                                                        }
                                                    }
                                                }
                                                .semantics { hideFromAccessibility() }
                                        } else {
                                            Modifier
                                        },
                                    ),
                            )

                            when (presentation) {
                                RootPresentation.Call -> FullScreenOverlay {
                                    CallScreen(state = callState, manager = manager)
                                }
                                is RootPresentation.IncomingOffer -> FullScreenOverlay {
                                    IncomingOfferScreen(
                                        onAccept = {
                                            if (presentation.offer.expiresAt <= System.currentTimeMillis()) {
                                                incomingOffer = null
                                                setOfferLockscreen(false)
                                                return@IncomingOfferScreen
                                            }
                                            val granted = ContextCompat.checkSelfPermission(
                                                this@MainActivity,
                                                Manifest.permission.RECORD_AUDIO,
                                            ) == PackageManager.PERMISSION_GRANTED
                                            if (granted) {
                                                alerts.dismiss(presentation.offer)
                                                incomingOffer = null
                                                manager.answerTakeover(
                                                    current,
                                                    presentation.offer.offerId,
                                                )
                                            } else {
                                                pendingMicrophoneOffer = presentation.offer
                                                micPermission.launch(Manifest.permission.RECORD_AUDIO)
                                            }
                                        },
                                        onDecline = {
                                            alerts.dismiss(presentation.offer)
                                            incomingOffer = null
                                            setOfferLockscreen(false)
                                        },
                                    )
                                }
                                RootPresentation.Main -> if (showingOverLockscreen) FullScreenOverlay {
                                    // 查询期间不把短信、记录等主界面内容暴露到锁屏上。
                                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                                        Text(stringResource(R.string.offer_checking))
                                    }
                                }
                                RootPresentation.Pairing -> Unit
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun FullScreenOverlay(content: @Composable () -> Unit) {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background),
    ) {
        content()
    }
}
