package ai.bondings.callpilot.ui

import java.io.IOException
import java.io.InterruptedIOException
import java.net.SocketTimeoutException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LineStatusStateTest {
    @Test
    fun `first failure without success is unavailable`() {
        val state = LineStatusState().onFailure(SocketTimeoutException("timeout"))

        assertEquals(false, state.ready)
        assertEquals("线路状态暂不可用", state.label)
        assertEquals("网络较慢，正在重试", state.error)
        assertFalse(state.failureTolerated)
        assertFalse(state.onFailure(IOException()).failureTolerated)
    }

    @Test
    fun `single failure after success preserves ready display and dialing`() {
        val success = LineStatusState().onSuccess(true, "远程拨号已就绪")
        val state = success.onFailure(SocketTimeoutException("timeout"))

        assertEquals(success.ready, state.ready)
        assertEquals(success.label, state.label)
        assertNull(state.error)
        assertTrue(state.failureTolerated)
        assertTrue(isDialEnabled("10086", state.ready))
    }

    @Test
    fun `second consecutive failure becomes unavailable and stays unavailable`() {
        val success = LineStatusState().onSuccess(true, "远程拨号已就绪")
        val state = success.onFailure(IOException()).onFailure(SocketTimeoutException("timeout"))

        assertEquals(false, state.ready)
        assertEquals("线路状态暂不可用", state.label)
        assertEquals("网络较慢，正在重试", state.error)
        assertFalse(state.failureTolerated)
        assertFalse(isDialEnabled("10086", state.ready))
        assertFalse(state.onFailure(IOException()).failureTolerated)
    }

    @Test
    fun `recovery clears error and resets failure tolerance`() {
        val failed = LineStatusState().onSuccess(true, "远程拨号已就绪")
            .onFailure(IOException()).onFailure(IOException())
        val recovered = failed.onSuccess(true, "远程拨号已就绪")

        assertEquals(true, recovered.ready)
        assertNull(recovered.error)
        assertEquals(0, recovered.consecutiveFailures)
        assertTrue(recovered.onFailure(IOException()).failureTolerated)
    }

    @Test
    fun `successful offline status is also preserved during tolerance`() {
        val state = LineStatusState().onSuccess(false, "SIM 线路离线").onFailure(IOException())

        assertEquals(false, state.ready)
        assertEquals("SIM 线路离线", state.label)
        assertNull(state.error)
        assertTrue(state.failureTolerated)
    }

    @Test
    fun `timeout and other failures have readable copy without raw details`() {
        assertEquals("网络较慢，正在重试", LineStatusState().onFailure(InterruptedIOException("timeout")).error)
        assertEquals("获取线路状态失败，请稍后重试", LineStatusState().onFailure(IOException("private address")).error)
    }
}
