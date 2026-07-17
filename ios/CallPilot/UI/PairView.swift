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
            Text(L10n.text("pair.title"))
                .font(.largeTitle).bold()
            Text(L10n.text("pair.subtitle"))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            TextField(L10n.text("pair.code_placeholder"), text: $code)
                .textFieldStyle(.roundedBorder)
                .textInputAutocapitalization(.characters)
                .autocorrectionDisabled()
            TextField(L10n.text("pair.gateway_placeholder"), text: $gateway)
                .textFieldStyle(.roundedBorder)
                .keyboardType(.URL)
                .autocorrectionDisabled()
            TextField(L10n.text("pair.device_name_placeholder"), text: $displayName)
                .textFieldStyle(.roundedBorder)

            Button {
                busy = true
                Task { await model.pair(code: code.trimmingCharacters(in: .whitespaces),
                                        gatewayURL: gateway, displayName: displayName)
                    busy = false }
            } label: {
                Text(L10n.text(busy ? "pair.action.busy" : "pair.action"))
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(busy || code.isEmpty)

            if let error = model.pairingError {
                Text(error).font(.footnote).foregroundStyle(.red)
            }
            Spacer()
            HStack(spacing: 20) {
                Link(L10n.text("settings.legal.privacy_policy"), destination: AppLinks.privacyPolicy)
                Link(L10n.text("settings.legal.terms"), destination: AppLinks.terms)
                Link(L10n.text("settings.legal.support"), destination: AppLinks.support)
            }
            .font(.footnote)
        }
        .padding(28)
    }
}
