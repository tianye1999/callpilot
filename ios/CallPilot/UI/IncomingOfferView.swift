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
            Text("来电转接请求").font(.largeTitle).bold()
            Text("AI 正在接听一通来电,判断需要你本人处理。\n接听后通话将转到这台手机。")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            Spacer()
            HStack(spacing: 24) {
                Button {
                    model.dismissOffer(offer)
                } label: {
                    Label("拒绝", systemImage: "phone.down.fill")
                        .frame(maxWidth: .infinity, minHeight: 60)
                }
                .buttonStyle(.borderedProminent).tint(.red)

                Button {
                    Task { await model.answerTakeover(offer) }
                } label: {
                    Label("接听", systemImage: "phone.fill")
                        .frame(maxWidth: .infinity, minHeight: 60)
                }
                .buttonStyle(.borderedProminent).tint(.green)
            }
            Text("拒绝后 AI 会继续处理这通电话。")
                .font(.footnote).foregroundStyle(.secondary)
        }
        .padding(28)
    }
}
