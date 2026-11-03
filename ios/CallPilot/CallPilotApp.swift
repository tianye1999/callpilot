import SwiftUI

@main
struct CallPilotApp: App {
    @UIApplicationDelegateAdaptor(AlertPushDelegate.self) private var alerts
    var body: some Scene {
        WindowGroup {
            RootView(alerts: alerts)
        }
    }
}

/// 根展示层:未配对时显示配对页;配对后保持主 Tab 壳常驻,通话与来电作为顶层覆盖。
struct RootView: View {
    @StateObject private var model = AppModel()
    @ObservedObject var alerts: AlertPushDelegate
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        let presentation = RootPresentation.resolve(
            isPaired: model.pairing != nil,
            callState: model.callState,
            incomingOffer: model.incomingOffer
        )

        Group {
            if presentation == .pairing {
                PairView(model: model)
            } else {
                ZStack {
                    MainTabShell(model: model)
                        .allowsHitTesting(presentation == .main)
                        .accessibilityHidden(presentation != .main)

                    switch presentation {
                    case .call:
                        fullScreenOverlay { CallView(model: model) }
                    case .incomingOffer(let offer):
                        fullScreenOverlay { IncomingOfferView(model: model, offer: offer) }
                    case .pairing, .main:
                        EmptyView()
                    }
                }
            }
        }
        .task { await model.startOfferPolling() }
        .task(id: model.pairing?.credential.deviceId) {
            guard model.pairing != nil else { return }
            model.prefetchOutboundIce()
            await refreshAlertRegistration()
            await openPendingAlert()
        }
        .task(id: alerts.token) {
            guard let token = alerts.token, alerts.authorized else { return }
            await model.syncAlertToken(token, authorized: true)
        }
        .onChange(of: alerts.pendingRoute?.id, initial: true) { _, _ in
            Task { await openPendingAlert() }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                model.prefetchOutboundIce()
                Task { await refreshAlertRegistration() }
            }
        }
    }

    private func refreshAlertRegistration() async {
        guard model.pairing != nil else { return }
        await alerts.refreshAuthorization()
        await model.syncAlertToken(alerts.token, authorized: alerts.authorized)
    }

    private func openPendingAlert() async {
        guard model.pairing != nil, let route = alerts.pendingRoute else { return }
        alerts.pendingRoute = nil
        await model.openAlert(route)
    }

    private func fullScreenOverlay<Content: View>(
        @ViewBuilder content: () -> Content
    ) -> some View {
        ZStack {
            Color(uiColor: .systemBackground).ignoresSafeArea()
            content()
        }
    }
}
