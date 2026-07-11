package ai.bondings.callpilot

import ai.bondings.callpilot.call.CallGraph
import ai.bondings.callpilot.call.CallState
import ai.bondings.callpilot.pairing.CredentialStore
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.ui.CallPilotTheme
import ai.bondings.callpilot.ui.CallScreen
import ai.bondings.callpilot.ui.DialScreen
import ai.bondings.callpilot.ui.PairScreen
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier

/** 导航：未配对 → 配对页；已配对空闲 → 拨号页；通话生命周期内 → 通话页。 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = CredentialStore(applicationContext)
        val manager = CallGraph.manager(applicationContext)
        setContent {
            CallPilotTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    var pairing by remember { mutableStateOf<StoredPairing?>(store.load()) }
                    val callState by manager.state.collectAsState()
                    val current = pairing
                    when {
                        current == null -> PairScreen(store = store, onPaired = { pairing = it })
                        callState is CallState.Idle -> DialScreen(
                            pairing = current,
                            store = store,
                            manager = manager,
                            onUnpaired = { pairing = null },
                        )
                        else -> CallScreen(state = callState, manager = manager)
                    }
                }
            }
        }
    }
}
