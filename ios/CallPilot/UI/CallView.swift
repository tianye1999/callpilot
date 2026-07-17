import SwiftUI

/// 通话页(对齐 Android CallScreen)。状态标签 + DTMF + 挂断;
/// 明确显示"正在通过远端 SIM 通话"(#30 体验设计:避免误以为是手机蜂窝线路)。
struct CallView: View {
    @ObservedObject var model: AppModel
    @State private var showKeypad = false

    private let keys = ["1","2","3","4","5","6","7","8","9","*","0","#"]

    var body: some View {
        VStack(spacing: 20) {
            Spacer()
            Text(statusText).font(.title2).bold()
            if !model.callState.isTerminal {
                Text(L10n.text("call.remote_sim_notice"))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Spacer()

            if model.callState.isTerminal {
                Button(L10n.text("call.return_to_dialer")) {
                    model.dismissCallResult()
                }
                .buttonStyle(.borderedProminent)
            } else {
                if showKeypad {
                    LazyVGrid(columns: Array(repeating: GridItem(.flexible()), count: 3), spacing: 14) {
                        ForEach(keys, id: \.self) { k in
                            Button(k) { model.sendDTMF(k) }
                                .font(.title).frame(width: 66, height: 66)
                                .background(Color.gray.opacity(0.12), in: Circle())
                        }
                    }
                }

                HStack(spacing: 28) {
                    Button { showKeypad.toggle() } label: {
                        Image(systemName: "circle.grid.3x3.fill").font(.title2)
                            .frame(width: 48, height: 48)
                    }
                    .accessibilityLabel(
                        L10n.text(showKeypad ? "call.keypad.hide" : "call.keypad.show")
                    )

                    Button {
                        model.setSpeakerphone(!model.speakerphoneEnabled)
                    } label: {
                        Image(systemName: model.speakerphoneEnabled ? "speaker.wave.2.fill" : "speaker.fill")
                            .font(.title2)
                            .frame(width: 48, height: 48)
                            .foregroundStyle(model.speakerphoneEnabled ? .blue : .primary)
                    }
                    .accessibilityLabel(
                        L10n.text(
                            model.speakerphoneEnabled
                                ? "call.speaker.disable"
                                : "call.speaker.enable"
                        )
                    )

                    Button { model.hangup() } label: {
                        Image(systemName: "phone.down.fill").font(.title)
                            .frame(width: 72, height: 72)
                            .background(.red, in: Circle()).foregroundStyle(.white)
                    }
                    .accessibilityLabel(L10n.text("call.hangup"))
                }
            }
            Spacer()
        }
        .padding(24)
    }

    private var statusText: String {
        switch model.callState {
        case .preparing(let label): return L10n.format("call.state.preparing", label)
        case .waitingMedia(let label): return L10n.format("call.state.waiting_media", label)
        case .dialing(let number): return L10n.format("call.state.dialing", number)
        case .inCall(let label): return L10n.format("call.state.in_call", label)
        case .ended(let label, _): return L10n.format("call.state.ended", label)
        case .failed(let label, _, let code):
            return L10n.format("call.state.failed", label, CallFailureCopy.message(code: code))
        case .idle: return L10n.text("call.state.idle")
        }
    }
}
