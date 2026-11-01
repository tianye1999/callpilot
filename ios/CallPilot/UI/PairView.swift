import SwiftUI

/// 配对页(对齐 Android PairScreen 的 hosted 路径)。
/// 支持电脑端配对链接或手动网关 + 配对码,claim 成功即存 Keychain。
struct PairView: View {
    @ObservedObject var model: AppModel
    @State private var code = ""
    @State private var gateway = PairingLink.defaultGateway
    @State private var displayName = UIDevice.current.name
    @State private var busy = false

    var body: some View {
        ScrollView {
            VStack(spacing: 20) {
                Text(L10n.text("pair.title"))
                    .font(.title2).bold()
                    .multilineTextAlignment(.center)
                Text(L10n.text("pair.subtitle"))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)

                TextField(L10n.text("pair.code_placeholder"), text: $code)
                    .textFieldStyle(.roundedBorder)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .onChange(of: code) { _, value in
                        let parsed = PairingLink.parse(value)
                        guard let normalized = parsed.code else { return }
                        if let origin = parsed.gatewayBase { gateway = origin }
                        code = PairingLink.formatCode(normalized)
                    }
                TextField(L10n.text("pair.gateway_placeholder"), text: $gateway)
                    .textFieldStyle(.roundedBorder)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                TextField(L10n.text("pair.device_name_placeholder"), text: $displayName)
                    .textFieldStyle(.roundedBorder)

                Button {
                    let parsed = PairingLink.parse(code)
                    guard let normalized = parsed.code,
                          let origin = parsed.gatewayBase ?? PairingLink.parse(gateway).gatewayBase else { return }
                    busy = true
                    Task {
                        await model.pair(code: PairingLink.formatCode(normalized),
                                         gatewayURL: origin, displayName: displayName)
                        busy = false
                    }
                } label: {
                    Text(L10n.text(busy ? "pair.action.busy" : "pair.action"))
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(busy || PairingLink.parse(code).code == nil
                          || PairingLink.parse(gateway).gatewayBase == nil)

                if let error = model.pairingError {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }
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
}
