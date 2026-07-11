package ai.bondings.callpilot

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * v0 脚手架入口。真实三页（配对 / 拨号 / 通话）在 M3 落地，
 * 结构见 docs/decisions/002-android-native-client.md。
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    ScaffoldPlaceholder()
                }
            }
        }
    }
}

@Composable
private fun ScaffoldPlaceholder() {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(text = "CallPilot", style = MaterialTheme.typography.headlineMedium)
        Text(
            text = "远程拨号原生客户端 · 脚手架 (#36)",
            style = MaterialTheme.typography.bodyMedium,
        )
    }
}
