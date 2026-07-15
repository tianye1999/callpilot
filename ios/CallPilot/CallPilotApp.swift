import SwiftUI

@main
struct CallPilotApp: App {
    var body: some Scene {
        WindowGroup {
            RootView()
        }
    }
}

/// 导航:未配对 → 配对页;已配对空闲 → 拨号页(含来电接管卡);通话生命周期内 → 通话页。
/// 对齐 Android MainActivity 的 when 分支。
struct RootView: View {
    @StateObject private var model = AppModel()

    var body: some View {
        Group {
            if model.pairing == nil {
                PairView(model: model)
            } else if let offer = model.incomingOffer, !model.callState.isActive {
                IncomingOfferView(model: model, offer: offer)
            } else if model.callState.isActive {
                CallView(model: model)
            } else {
                DialView(model: model)
            }
        }
        .task { await model.startOfferPolling() }
    }
}
