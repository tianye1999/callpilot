package ai.bondings.callpilot

import ai.bondings.callpilot.pairing.CredentialStore
import ai.bondings.callpilot.pairing.StoredPairing
import ai.bondings.callpilot.ui.DialScreen
import ai.bondings.callpilot.ui.PairScreen
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier

/** v0 导航：未配对 → 配对页；已配对 → 拨号页。 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = CredentialStore(applicationContext)
        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    var pairing by remember { mutableStateOf<StoredPairing?>(store.load()) }
                    val current = pairing
                    if (current == null) {
                        PairScreen(store = store, onPaired = { pairing = it })
                    } else {
                        DialScreen(
                            pairing = current,
                            store = store,
                            onUnpaired = { pairing = null },
                        )
                    }
                }
            }
        }
    }
}
