import SwiftUI

/// 配对页(对齐 Android PairScreen 的 hosted 路径)。
/// 前台版:手输配对码 + 网关地址(Beta),claim 成功即存 Keychain。
struct PairView: View {
    @ObservedObject var model: AppModel
    @State private var code = ""
    @State private var gateway = "https://dial-beta.bondings.ai"
    @State private var displayName = UIDevice.current.name
    @State private var busy = false

    var body: some View {
        VStack(spacing: 20) {
            Text("配对 CallPilot")
                .font(.largeTitle).bold()
            Text("在电脑端 CallPilot 生成配对码后输入。")
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            TextField("配对码(如 ABCD-EFGH)", text: $code)
                .textFieldStyle(.roundedBorder)
                .textInputAutocapitalization(.characters)
                .autocorrectionDisabled()
            TextField("网关地址", text: $gateway)
                .textFieldStyle(.roundedBorder)
                .keyboardType(.URL)
                .autocorrectionDisabled()
            TextField("设备名", text: $displayName)
                .textFieldStyle(.roundedBorder)

            Button {
                busy = true
                Task { await model.pair(code: code.trimmingCharacters(in: .whitespaces),
                                        gatewayURL: gateway, displayName: displayName)
                    busy = false }
            } label: {
                Text(busy ? "配对中…" : "配对").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(busy || code.isEmpty)

            if !model.lineStatusLabel.hasPrefix("线路") {
                Text(model.lineStatusLabel).font(.footnote).foregroundStyle(.red)
            }
            Spacer()
        }
        .padding(28)
    }
}
