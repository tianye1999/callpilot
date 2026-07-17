import SwiftUI

/// 来电接管请求全屏卡(对齐 Android IncomingOfferScreen)。
/// 前台展示与系统 CallKit 来电界面共享同一 offer 状态。
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
