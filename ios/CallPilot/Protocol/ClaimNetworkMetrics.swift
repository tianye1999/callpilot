import Foundation

final class ClaimNetworkMetrics: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    struct Transaction: Sendable {
        var dnsStart: Date?
        var dnsEnd: Date?
        var connectStart: Date?
        var connectEnd: Date?
        var tlsStart: Date?
        var tlsEnd: Date?
        var requestStart: Date?
        var firstByte: Date?
        var responseEnd: Date?
        var fetchStart: Date?
        var reused = false
    }

    private let wallTime: Date
    private let monotonicTime: TimeInterval
    private let lock = NSLock()
    private var recorded = false
    private var values: [CallTimingMark: TimeInterval] = [:]

    init(wallTime: Date = Date(), monotonicTime: TimeInterval = CallMonotonic.now()) {
        self.wallTime = wallTime
        self.monotonicTime = monotonicTime
    }

    var marks: [CallTimingMark: TimeInterval] {
        lock.lock()
        defer { lock.unlock() }
        return values
    }

    func record(_ transaction: Transaction) {
        lock.lock()
        defer { lock.unlock() }
        guard !recorded else { return }
        recorded = true
        let dates: [(CallTimingMark, Date?)] = [
            (.claimDNSStart, transaction.dnsStart), (.claimDNSEnd, transaction.dnsEnd),
            (.claimConnectStart, transaction.connectStart), (.claimConnectEnd, transaction.connectEnd),
            (.claimTLSStart, transaction.tlsStart), (.claimTLSEnd, transaction.tlsEnd),
            (.claimRequestStart, transaction.requestStart), (.claimFirstByte, transaction.firstByte),
            (.claimResponseEnd, transaction.responseEnd),
            (.claimConnectionReused, transaction.reused ? transaction.requestStart ?? transaction.fetchStart : nil),
        ]
        for (stage, date) in dates {
            if let date {
                let instant = monotonicTime + date.timeIntervalSince(wallTime)
                if instant.isFinite { values[stage] = instant }
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didFinishCollecting metrics: URLSessionTaskMetrics) {
        // One transaction, not a mixture of timestamps from redirects or retries.
        guard let first = metrics.transactionMetrics.first else { return }
        record(Transaction(
            dnsStart: first.domainLookupStartDate, dnsEnd: first.domainLookupEndDate,
            connectStart: first.connectStartDate, connectEnd: first.connectEndDate,
            tlsStart: first.secureConnectionStartDate, tlsEnd: first.secureConnectionEndDate,
            requestStart: first.requestStartDate, firstByte: first.responseStartDate,
            responseEnd: first.responseEndDate,
            fetchStart: first.fetchStartDate, reused: first.isReusedConnection
        ))
    }
}
