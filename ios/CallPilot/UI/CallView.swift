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
                Text("正在通过远端 SIM 通话").font(.footnote).foregroundStyle(.secondary)
            }
            Spacer()

            if model.callState.isTerminal {
                Button("返回拨号") {
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
                    .accessibilityLabel(showKeypad ? "隐藏键盘" : "显示键盘")

                    Button {
                        model.setSpeakerphone(!model.speakerphoneEnabled)
                    } label: {
                        Image(systemName: model.speakerphoneEnabled ? "speaker.wave.2.fill" : "speaker.fill")
                            .font(.title2)
                            .frame(width: 48, height: 48)
                            .foregroundStyle(model.speakerphoneEnabled ? .blue : .primary)
                    }
                    .accessibilityLabel(model.speakerphoneEnabled ? "关闭扬声器" : "打开扬声器")

                    Button { model.hangup() } label: {
                        Image(systemName: "phone.down.fill").font(.title)
                            .frame(width: 72, height: 72)
                            .background(.red, in: Circle()).foregroundStyle(.white)
                    }
                    .accessibilityLabel("挂断")
                }
            }
            Spacer()
        }
        .padding(24)
    }

    private var statusText: String {
        switch model.callState {
        case .preparing(let l): return "\(l) · 准备中"
        case .waitingMedia(let l): return "\(l) · 建立媒体中"
        case .dialing(let n): return "\(n) · 拨号中"
        case .inCall(let l): return "\(l) · 通话中"
        case .ended(let l, let r): return "\(l) · 已结束(\(r))"
        case .failed(let l, let r, _): return "\(l) · 失败:\(r)"
        case .idle: return "空闲"
        }
    }
}
