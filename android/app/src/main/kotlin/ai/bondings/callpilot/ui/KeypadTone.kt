package ai.bondings.callpilot.ui

import android.content.Context
import android.media.AudioManager
import android.media.ToneGenerator
import android.provider.Settings

internal class KeypadTone(private val context: Context) {
    private var generator: ToneGenerator? = null

    fun play(digit: String) {
        val tone = toneFor(digit) ?: return
        // Feedback is best effort; audio allocation must never break input/signaling.
        runCatching {
            if (Settings.System.getInt(
                    context.contentResolver, Settings.System.DTMF_TONE_WHEN_DIALING, 1,
                ) != 1) return
            val player = generator ?: ToneGenerator(AudioManager.STREAM_DTMF, 80)
                .also { generator = it }
            player.startTone(tone, 150)
        }
    }

    fun release() {
        val player = generator
        generator = null
        runCatching { player?.release() }
    }

    companion object {
        fun toneFor(digit: String): Int? = when (digit) {
            "0" -> ToneGenerator.TONE_DTMF_0
            "1" -> ToneGenerator.TONE_DTMF_1
            "2" -> ToneGenerator.TONE_DTMF_2
            "3" -> ToneGenerator.TONE_DTMF_3
            "4" -> ToneGenerator.TONE_DTMF_4
            "5" -> ToneGenerator.TONE_DTMF_5
            "6" -> ToneGenerator.TONE_DTMF_6
            "7" -> ToneGenerator.TONE_DTMF_7
            "8" -> ToneGenerator.TONE_DTMF_8
            "9" -> ToneGenerator.TONE_DTMF_9
            "*" -> ToneGenerator.TONE_DTMF_S
            "#" -> ToneGenerator.TONE_DTMF_P
            else -> null
        }
    }
}
