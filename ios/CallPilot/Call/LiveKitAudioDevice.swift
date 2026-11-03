@preconcurrency import AVFAudio
import LiveKit
import OSLog

@MainActor
final class LiveKitAudioDevice: CallAudioDevice {
    static func prepareForIncoming() {
        let log = Logger(subsystem: "ai.bondings.callpilot.sim", category: "CallAudio")
        do { try LiveKitAudioDevice().configureCallKitSession() }
        catch { log.error("incoming category failed: \(error.localizedDescription, privacy: .public)") }
        AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = false
        do { try AudioManager.shared.setEngineAvailability(.none) }
        catch { log.error("incoming engine gate failed: \(error.localizedDescription, privacy: .public)") }
    }
    static let owner = CallAudioOwner(device: LiveKitAudioDevice()) { error in
        Logger(subsystem: "ai.bondings.callpilot.sim", category: "CallAudio")
            .error("Audio cleanup failed: \(error.localizedDescription, privacy: .public)")
    }

    func prepare(mode: CallAudioMode) throws {
        try stop()
        AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = mode == .standalone
        if mode == .standalone {
            try AudioManager.shared.setEngineAvailability(.default)
        }
    }

    func configureCallKitSession() throws {
        try AVAudioSession.sharedInstance().setCategory(
            .playAndRecord, mode: .voiceChat, options: [.mixWithOthers]
        )
    }

    func activate() throws {
        do {
            if !AudioManager.shared.audioSession.isAutomaticConfigurationEnabled {
                try configureCallKitSession()
            }
            try AudioManager.shared.setEngineAvailability(.default)
        } catch {
            Logger(subsystem: "ai.bondings.callpilot.sim", category: "CallAudio")
                .error("CallKit audio activation failed: \(error.localizedDescription, privacy: .public)")
            try? AudioManager.shared.setEngineAvailability(.none)
            throw error
        }
    }

    func stop() throws {
        // Attempt both even if the first native call fails. Availability alone
        // only suspends recording; it does not clear the ADM recording request.
        var failure: Error?
        do { try AudioManager.shared.setEngineAvailability(.none) }
        catch { failure = error }
        do { try AudioManager.shared.stopLocalRecording() }
        catch { failure = failure ?? error }
        if let failure { throw failure }
        // Do not change automatic configuration or setActive while CallKit owns it.
    }
}
