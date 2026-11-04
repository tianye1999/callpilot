package ai.bondings.callpilot.protocol

import ai.bondings.callpilot.call.OutboundTiming
import ai.bondings.callpilot.call.OutboundTiming.Mark
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Proxy
import okhttp3.Call
import okhttp3.Connection
import okhttp3.EventListener
import okhttp3.Handshake
import okhttp3.Response

/** Records timestamps only, never addresses, URLs, headers or request/response bodies. */
internal class OutboundHttpTiming(private val timing: OutboundTiming) : EventListener() {
    override fun dnsStart(call: Call, domainName: String) = timing.mark(Mark.DNS_START)
    override fun dnsEnd(call: Call, domainName: String, inetAddressList: List<InetAddress>) = timing.mark(Mark.DNS_END)
    override fun connectStart(call: Call, inetSocketAddress: InetSocketAddress, proxy: Proxy) = timing.mark(Mark.TCP_START)
    override fun secureConnectStart(call: Call) = timing.mark(Mark.TLS_START)
    override fun secureConnectEnd(call: Call, handshake: Handshake?) = timing.mark(Mark.TLS_END)
    override fun connectionAcquired(call: Call, connection: Connection) = timing.mark(Mark.CONNECTION)
    override fun requestBodyEnd(call: Call, byteCount: Long) = timing.mark(Mark.POST_SENT)
    override fun responseHeadersEnd(call: Call, response: Response) {
        timing.mark(Mark.RESPONSE_HEADERS)
        timing.serverTiming(response.header("Server-Timing"))
    }
}
