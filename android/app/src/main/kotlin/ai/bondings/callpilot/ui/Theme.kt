package ai.bondings.callpilot.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.size
import androidx.compose.ui.unit.dp

/** CallPilot 品牌色：蓝→紫渐变，呼应桌面控制台的渐变 primary。 */
object Brand {
    val Blue = Color(0xFF2E5CE6)
    val Violet = Color(0xFF6C4DF6)
    val gradient = Brush.linearGradient(listOf(Blue, Violet))
    val Green = Color(0xFF23A55A)
}

private val LightColors = lightColorScheme(
    primary = Brand.Blue,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFDDE5FF),
    onPrimaryContainer = Color(0xFF0A2472),
    secondary = Brand.Violet,
    surface = Color(0xFFFBFBFE),
    surfaceVariant = Color(0xFFEEF0F8),
    background = Color(0xFFFBFBFE),
    outline = Color(0xFFC4C9D6),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFFB3C4FF),
    onPrimary = Color(0xFF0A2472),
    primaryContainer = Color(0xFF1E3A9F),
    secondary = Color(0xFFCBBEFF),
    surface = Color(0xFF121319),
    surfaceVariant = Color(0xFF23252E),
    background = Color(0xFF121319),
)

@Composable
fun CallPilotTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors,
        content = content,
    )
}

/** 品牌渐变主按钮（禁用/忙碌态自动降级）。 */
@Composable
fun GradientButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    busy: Boolean = false,
) {
    val shape = RoundedCornerShape(14.dp)
    val active = enabled && !busy
    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(52.dp)
            .clip(shape)
            .background(
                if (active) Brand.gradient
                else Brush.linearGradient(
                    listOf(
                        MaterialTheme.colorScheme.surfaceVariant,
                        MaterialTheme.colorScheme.surfaceVariant,
                    ),
                ),
            )
            .clickable(enabled = active, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        if (busy) {
            CircularProgressIndicator(
                modifier = Modifier.size(24.dp),
                color = MaterialTheme.colorScheme.primary,
                strokeWidth = 2.5.dp,
            )
        } else {
            Text(
                text,
                style = MaterialTheme.typography.titleMedium,
                color = if (active) Color.White else MaterialTheme.colorScheme.outline,
            )
        }
    }
}
