package ai.bondings.callpilot

import ai.bondings.callpilot.call.CallGraph
import ai.bondings.callpilot.call.CallState
import ai.bondings.callpilot.pairing.CredentialStore
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.protocol.HostedCloudClient
import ai.bondings.callpilot.protocol.InboundOffer
import ai.bondings.callpilot.protocol.PairingProtocol
import ai.bondings.callpilot.ui.CallPilotTheme
import ai.bondings.callpilot.ui.CallScreen
import ai.bondings.callpilot.ui.IncomingOfferScreen
import ai.bondings.callpilot.ui.MainTabShell
import ai.bondings.callpilot.ui.PairScreen
import ai.bondings.callpilot.ui.RootPresentation
import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.semantics.hideFromAccessibility
import androidx.compose.ui.semantics.semantics
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

private const val OFFER_POLL_INTERVAL_MS = 3_000L

/** Root host: the paired tab shell remains mounted while call/offer UI overlays it. */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val store = CredentialStore(applicationContext)
        val manager = CallGraph.manager(applicationContext)
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
                    val dismissedOffers = remember { mutableSetOf<String>() }
                    val current = pairing

                    // #95：hosted 配对且前台空闲时轮询可接管的来电 offer（MVP=前台在线）。
                    LaunchedEffect(current) {
                        if (current != null && current.protocol == PairingProtocol.HOSTED) {
                            val offerClient = HostedCloudClient(current.gatewayUrl).also {
                                it.credential = current.credential
                            }
                            lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
                                while (true) {
                                    if (manager.state.value is CallState.Idle) {
                                        val offer = runCatching {
                                            withContext(Dispatchers.IO) {
                                                offerClient.listInboundOffers()
                                            }
                                        }.getOrDefault(emptyList()).firstOrNull {
                                            it.offerId !in dismissedOffers &&
                                                it.expiresAt > System.currentTimeMillis()
                                        }
                                        incomingOffer = offer
                                    } else {
                                        incomingOffer = null
                                    }
                                    delay(OFFER_POLL_INTERVAL_MS)
                                }
                            }
                        } else {
                            incomingOffer = null
                        }
                    }

                    val activeOffer = incomingOffer
                    val micPermission = rememberLauncherForActivityResult(
                        ActivityResultContracts.RequestPermission(),
                    ) { granted ->
                        val offer = incomingOffer
                        val paired = pairing
                        if (granted && offer != null && paired != null) {
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
                        val overlayVisible = presentation != RootPresentation.Main
                        Box(Modifier.fillMaxSize()) {
                            MainTabShell(
                                pairing = current,
                                store = store,
                                manager = manager,
                                onUnpaired = {
                                    incomingOffer = null
                                    pairing = null
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
                                            val granted = ContextCompat.checkSelfPermission(
                                                this@MainActivity,
                                                Manifest.permission.RECORD_AUDIO,
                                            ) == PackageManager.PERMISSION_GRANTED
                                            if (granted) {
                                                incomingOffer = null
                                                manager.answerTakeover(
                                                    current,
                                                    presentation.offer.offerId,
                                                )
                                            } else {
                                                micPermission.launch(Manifest.permission.RECORD_AUDIO)
                                            }
                                        },
                                        onDecline = {
                                            dismissedOffers += presentation.offer.offerId
                                            incomingOffer = null
                                        },
                                    )
                                }
                                RootPresentation.Main, RootPresentation.Pairing -> Unit
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
