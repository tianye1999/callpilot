import Combine
import UIKit
@preconcurrency import UserNotifications

@MainActor
final class AlertPushDelegate: NSObject, ObservableObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    @Published private(set) var token: String?
    @Published private(set) var authorized = false
    @Published var pendingRoute: AlertPushRoute?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        if AlertPushConfiguration.isEnabled {
            UNUserNotificationCenter.current().delegate = self
        }
        return true
    }

    func refreshAuthorization() async {
        guard AlertPushConfiguration.isEnabled else { return }
        let center = UNUserNotificationCenter.current()
        var settings = await center.notificationSettings()
        if settings.authorizationStatus == .notDetermined {
            _ = try? await center.requestAuthorization(options: [.alert, .sound, .badge])
            settings = await center.notificationSettings()
        }
        authorized = [.authorized, .provisional, .ephemeral].contains(settings.authorizationStatus)
        if authorized {
            UIApplication.shared.registerForRemoteNotifications()
        } else {
            token = nil
        }
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        guard AlertPushConfiguration.isEnabled else { return }
        token = deviceToken.map { String(format: "%02x", $0) }.joined()
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        // Registration is best effort; the next foreground activation retries.
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        guard AlertPushConfiguration.isEnabled,
              AlertPushRoute.decode(notification.request.content.userInfo) != nil else { return [] }
        return [.banner, .list, .sound]
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        guard AlertPushConfiguration.isEnabled,
              response.actionIdentifier == UNNotificationDefaultActionIdentifier,
              let route = AlertPushRoute.decode(response.notification.request.content.userInfo) else { return }
        await MainActor.run { self.pendingRoute = route }
    }
}
