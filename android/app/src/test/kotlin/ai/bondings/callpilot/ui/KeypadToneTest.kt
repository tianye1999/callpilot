package ai.bondings.callpilot.ui

import android.media.ToneGenerator
import org.junit.Assert.*
import org.junit.Test

class KeypadToneTest {
    @Test
    fun `all twelve keys map to distinct DTMF tones`() {
        val tones = "0123456789*#".map { KeypadTone.toneFor(it.toString()) }
        assertEquals(12, tones.filterNotNull().toSet().size)
        assertEquals(ToneGenerator.TONE_DTMF_0, KeypadTone.toneFor("0"))
        assertEquals(ToneGenerator.TONE_DTMF_S, KeypadTone.toneFor("*"))
        assertEquals(ToneGenerator.TONE_DTMF_P, KeypadTone.toneFor("#"))
        assertNull(KeypadTone.toneFor("+"))
        assertNull(KeypadTone.toneFor("12"))
    }
}
