import SwiftUI

/// 来电接管请求全屏卡(对齐 Android IncomingOfferScreen)。
/// 前台版:App 前台时展示;锁屏/系统来电 UI 属 Phase 2(CallKit)。
struct IncomingOfferView: View {
    @ObservedObject var model: AppModel
    let offer: InboundOffer

    var body: some View {
        VStack(spacing: 18) {
            Spacer()
            Image(systemName: "phone.arrow.up.right.fill")
                .font(.system(size: 56)).foregroundStyle(.green)
            Text(L10n.text("incoming.title")).font(.largeTitle).bold()
            Text(L10n.text("incoming.description"))
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            Spacer()
            HStack(spacing: 24) {
                Button {
                    model.dismissOffer(offer)
                } label: {
                    Label(L10n.text("incoming.decline"), systemImage: "phone.down.fill")
                        .frame(maxWidth: .infinity, minHeight: 60)
                }
                .buttonStyle(.borderedProminent).tint(.red)

                Button {
                    Task { await model.answerTakeover(offer) }
                } label: {
                    Label(L10n.text("incoming.answer"), systemImage: "phone.fill")
                        .frame(maxWidth: .infinity, minHeight: 60)
                }
                .buttonStyle(.borderedProminent).tint(.green)
            }
            Text(L10n.text("incoming.decline_footer"))
                .font(.footnote).foregroundStyle(.secondary)
        }
        .padding(28)
    }
}
