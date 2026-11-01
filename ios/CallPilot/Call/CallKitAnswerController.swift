import Foundation

@MainActor
protocol CallKitAnswerAction: AnyObject {
    var deadline: Date { get }
    var isComplete: Bool { get }
    func fulfill()
    func fail()
}

/// Platform-independent action deadlines and audio epochs. CallKit audio
/// callbacks have no UUID: retain ended epochs only for a bounded grace period.
@MainActor
final class CallKitAnswerController {
    private struct Entry {
        var action: (any CallKitAnswerAction)?
        var fulfilled = false
        var activated = false
        var failed = false
        var deadlineTask: Task<Void, Never>?
        var activationTask: Task<Void, Never>?
        var stopAudio: () -> Void = {}
    }

    private struct Drain {
        let endedAt: TimeInterval
        let expiresAt: TimeInterval
        let stopAudio: () -> Void
        var task: Task<Void, Never>?
    }

    private let now: () -> Date
    private let monotonicNow: () -> TimeInterval
    private let activationTimeout: Duration
    private let activationGrace: TimeInterval
    private let deactivationGrace: TimeInterval
    private let drainWaitTimeout: TimeInterval
    private let sleep: (Duration) async throws -> Void
    private let configureAudio: (UUID) throws -> Void
    private let activateAudio: (UUID) throws -> Void
    private let onFailure: (UUID, String) -> Void
    private let onDeactivate: (UUID) -> Void
    private var entries: [UUID: Entry] = [:]
    private var awaitingActivation: [UUID] = []
    private var awaitingDeactivation: [UUID] = []
    private var drains: [UUID: Drain] = [:]

    var hasUnresolvedAudioEvents: Bool {
        expireEndedAudio()
        return !awaitingActivation.isEmpty || !awaitingDeactivation.isEmpty
    }

    init(
        now: @escaping () -> Date = Date.init,
        monotonicNow: (() -> TimeInterval)? = nil,
        activationTimeout: Duration = .seconds(10),
        activationGrace: TimeInterval = 2,
        deactivationGrace: TimeInterval = 3,
        drainWaitTimeout: TimeInterval = 3,
        sleep: @escaping (Duration) async throws -> Void = { try await Task.sleep(for: $0) },
        configureAudio: @escaping (UUID) throws -> Void,
        activateAudio: @escaping (UUID) throws -> Void,
        onFailure: @escaping (UUID, String) -> Void,
        onDeactivate: @escaping (UUID) -> Void
    ) {
        self.now = now
        // Match Task.sleep's ContinuousClock, including time spent suspended.
        let clockOrigin = ContinuousClock.now
        self.monotonicNow = monotonicNow ?? {
            let elapsed = clockOrigin.duration(to: .now).components
            return Double(elapsed.seconds) + Double(elapsed.attoseconds) / 1e18
        }
        self.activationTimeout = activationTimeout
        self.activationGrace = activationGrace
        self.deactivationGrace = deactivationGrace
        self.drainWaitTimeout = drainWaitTimeout
        self.sleep = sleep
        self.configureAudio = configureAudio
        self.activateAudio = activateAudio
        self.onFailure = onFailure
        self.onDeactivate = onDeactivate
    }

    func begin(callID: UUID, action: any CallKitAnswerAction) {
        guard entries[callID] == nil else { return }
        let deadline = action.deadline
        entries[callID] = Entry(action: action)
        entries[callID]?.deadlineTask = Task { [weak self] in
            guard let self else { return }
            do { try await sleep(.seconds(max(0, deadline.timeIntervalSince(now())))) }
            catch { return }
            timedOut(callID: callID)
        }
    }

    func claimSucceeded(callID: UUID, stopAudio: @escaping () -> Void = {}) throws {
        guard let entry = entries[callID], !entry.failed else { throw CancellationError() }
        guard !entry.fulfilled else { return }
        guard let action = entry.action else { throw CancellationError() }
        guard now() < action.deadline, !action.isComplete else {
            timedOut(callID: callID)
            throw CallAudioError.answerExpired
        }
        try configureAudio(callID)
        // Configuration is synchronous but can take time in the audio subsystem.
        guard now() < action.deadline, !action.isComplete else {
            timedOut(callID: callID)
            throw CallAudioError.answerExpired
        }
        entries[callID]?.deadlineTask?.cancel()
        entries[callID]?.deadlineTask = nil
        entries[callID]?.action = nil
        entries[callID]?.fulfilled = true
        entries[callID]?.stopAudio = stopAudio
        awaitingActivation.append(callID)
        action.fulfill()
        entries[callID]?.activationTask = Task { [weak self] in
            guard let self else { return }
            do { try await sleep(activationTimeout) }
            catch { return }
            activationTimedOut(callID: callID)
        }
    }

    func timedOut(callID: UUID) {
        guard let entry = entries[callID], !entry.fulfilled, !entry.failed else { return }
        // A timed-out action must never be completed again, including by end().
        entries[callID]?.action = nil
        fail(callID, code: "CALLKIT_ACTION_TIMEOUT")
    }

    func activationTimedOut(callID: UUID) {
        guard let entry = entries[callID], entry.fulfilled, !entry.activated else { return }
        fail(callID, code: "CALLKIT_ACTIVATION_TIMEOUT")
    }

    func didActivate() {
        expireEndedAudio()
        guard !awaitingActivation.isEmpty else { return }
        let id = awaitingActivation.removeFirst()
        awaitingDeactivation.append(id)
        guard let entry = entries[id], !entry.failed else {
            scheduleDrain(id)
            return
        }
        do {
            try activateAudio(id)
            entries[id]?.activated = true
            entries[id]?.activationTask?.cancel()
            entries[id]?.activationTask = nil
        } catch {
            fail(id, code: "CALLKIT_AUDIO_ACTIVATION_FAILED")
        }
    }

    func didDeactivate() {
        expireEndedAudio()
        guard !awaitingDeactivation.isEmpty else { return }
        let id = awaitingDeactivation.removeFirst()
        finishDrain(id)
        guard let entry = entries[id], !entry.failed else { return }
        onDeactivate(id)
    }

    /// Returns whether the coordinator should reportCall(ended:). A pending
    /// action is failed instead, never both failed and reported by this path.
    @discardableResult
    func end(callID: UUID) -> Bool {
        guard let entry = entries.removeValue(forKey: callID) else { return true }
        entry.deadlineTask?.cancel()
        entry.activationTask?.cancel()
        scheduleDrain(callID, stopAudio: entry.stopAudio)
        if let action = entry.action, !action.isComplete, now() < action.deadline {
            action.fail()
            return false
        }
        return true
    }

    func reset() {
        for id in Array(entries.keys) { end(callID: id) }
        for id in Array(drains.keys) { finishDrain(id) }
        awaitingActivation.removeAll()
        awaitingDeactivation.removeAll()
    }

    private func fail(_ id: UUID, code: String) {
        guard let entry = entries[id], !entry.failed else { return }
        entries[id]?.failed = true
        entries[id]?.deadlineTask?.cancel()
        entries[id]?.activationTask?.cancel()
        scheduleDrain(id, stopAudio: entry.stopAudio)
        onFailure(id, code)
    }

    /// Both outbound admission and incoming answers use the same bounded gate.
    /// Revalidate the answer after every suspension; never outwait its deadline.
    func waitUntilAudioDrained(answerID: UUID? = nil) async -> Bool {
        let waitDeadline = monotonicNow() + drainWaitTimeout
        while true {
            guard !Task.isCancelled else { return false }
            var remaining = waitDeadline - monotonicNow()
            if let answerID {
                guard let entry = entries[answerID], !entry.failed,
                      let action = entry.action, !action.isComplete else { return false }
                guard now() < action.deadline else {
                    timedOut(callID: answerID)
                    return false
                }
                remaining = min(remaining, action.deadline.timeIntervalSince(now()))
            }
            if !hasUnresolvedAudioEvents { return true }
            guard remaining > 0 else { return false }
            do { try await sleep(.seconds(min(0.02, remaining))) }
            catch { return false }
        }
    }

    private func scheduleDrain(_ id: UUID, stopAudio: (() -> Void)? = nil) {
        let waitingForActivation = awaitingActivation.contains(id)
        guard waitingForActivation || awaitingDeactivation.contains(id) else { return }
        let existing = drains[id]
        let endedAt = existing?.endedAt ?? monotonicNow()
        // A late activation gets only the remainder of the original 3 s budget,
        // not a fresh 3 s extension on top of the activation grace.
        let deadline = endedAt + (waitingForActivation ? activationGrace : deactivationGrace)
        existing?.task?.cancel()
        drains[id] = Drain(endedAt: endedAt, expiresAt: deadline,
                           stopAudio: existing?.stopAudio ?? stopAudio ?? {})
        drains[id]?.task = Task { [weak self] in
            guard let self else { return }
            do { try await sleep(.seconds(max(0, deadline - monotonicNow()))) }
            catch { return }
            expireEndedAudio()
        }
    }

    private func expireEndedAudio() {
        for (id, drain) in drains where drain.expiresAt <= monotonicNow() {
            finishDrain(id)
        }
    }

    private func finishDrain(_ id: UUID) {
        guard let drain = drains.removeValue(forKey: id) else { return }
        drain.task?.cancel()
        awaitingActivation.removeAll { $0 == id }
        awaitingDeactivation.removeAll { $0 == id }
        // Captures the ended session, never whichever session is now current.
        drain.stopAudio()
    }
}
