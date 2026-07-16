import SwiftUI

enum MainTab: String, CaseIterable {
    case dial
    case records
    case messages
    case settings
}

/// 配对后的常驻导航壳。每个 Tab 独立持有导航路径,顶层来电/通话覆盖不会销毁其状态。
struct MainTabShell: View {
    @ObservedObject var model: AppModel

    @State private var selectedTab: MainTab = .dial
    @State private var dialPath = NavigationPath()
    @State private var recordsPath = NavigationPath()
    @State private var messagesPath = NavigationPath()
    @State private var settingsPath = NavigationPath()

    var body: some View {
        TabView(selection: $selectedTab) {
            NavigationStack(path: $dialPath) {
                DialView(model: model)
                    .navigationTitle(L10n.text("tab.dial"))
            }
            .tabItem { Label(L10n.text("tab.dial"), systemImage: "phone") }
            .tag(MainTab.dial)

            NavigationStack(path: $recordsPath) {
                if let history = model.callHistory {
                    CallRecordsView(model: history)
                } else {
                    ContentUnavailableView(
                        L10n.text("calls.load_failed"),
                        systemImage: "clock.badge.exclamationmark"
                    )
                    .navigationTitle(L10n.text("tab.records"))
                }
            }
            .tabItem { Label(L10n.text("tab.records"), systemImage: "clock") }
            .tag(MainTab.records)

            NavigationStack(path: $messagesPath) {
                if let inbox = model.messageInbox {
                    MessagesView(model: inbox)
                } else {
                    ContentUnavailableView(
                        L10n.text("messages.load_failed"),
                        systemImage: "message.badge.filled.fill"
                    )
                    .navigationTitle(L10n.text("tab.messages"))
                }
            }
            .tabItem { Label(L10n.text("tab.messages"), systemImage: "message") }
            .badge(model.messageInbox?.unreadCount ?? 0)
            .tag(MainTab.messages)

            NavigationStack(path: $settingsPath) {
                SettingsView(model: model)
                    .navigationTitle(L10n.text("tab.settings"))
            }
            .tabItem { Label(L10n.text("tab.settings"), systemImage: "gearshape") }
            .tag(MainTab.settings)
        }
    }
}
