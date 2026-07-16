package ai.bondings.callpilot.ui

import androidx.compose.ui.graphics.Color
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow

class IncomingOfferScreenTest {

    @Test
    fun `incoming action colors meet WCAG AA contrast`() {
        assertContrast(IncomingOfferColors.reject.container, IncomingOfferColors.reject.content)
        assertContrast(IncomingOfferColors.accept.container, IncomingOfferColors.accept.content)
    }

    private fun assertContrast(background: Color, foreground: Color) {
        val ratio = contrastRatio(background, foreground)
        assertTrue("expected contrast >= 4.5, got $ratio", ratio >= 4.5)
    }

    private fun contrastRatio(first: Color, second: Color): Double {
        val firstLuminance = relativeLuminance(first)
        val secondLuminance = relativeLuminance(second)
        return (max(firstLuminance, secondLuminance) + 0.05) /
            (min(firstLuminance, secondLuminance) + 0.05)
    }

    private fun relativeLuminance(color: Color): Double =
        0.2126 * linearize(color.red) +
            0.7152 * linearize(color.green) +
            0.0722 * linearize(color.blue)

    private fun linearize(channel: Float): Double {
        val value = channel.toDouble()
        return if (value <= 0.04045) value / 12.92 else ((value + 0.055) / 1.055).pow(2.4)
    }
}
