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
                    .navigationTitle("拨号")
            }
            .tabItem { Label("拨号", systemImage: "phone") }
            .tag(MainTab.dial)

            NavigationStack(path: $recordsPath) {
                ContentUnavailableView("暂无通话记录", systemImage: "clock.arrow.circlepath")
                    .navigationTitle("记录")
            }
            .tabItem { Label("记录", systemImage: "clock") }
            .tag(MainTab.records)

            NavigationStack(path: $messagesPath) {
                if let inbox = model.messageInbox {
                    MessagesView(model: inbox)
                } else {
                    ContentUnavailableView("无法载入短信", systemImage: "message.badge.filled.fill")
                        .navigationTitle("短信")
                }
            }
            .tabItem { Label("短信", systemImage: "message") }
            .badge(model.messageInbox?.unreadCount ?? 0)
            .tag(MainTab.messages)

            NavigationStack(path: $settingsPath) {
                SettingsView(model: model)
                    .navigationTitle("设置")
            }
            .tabItem { Label("设置", systemImage: "gearshape") }
            .tag(MainTab.settings)
        }
    }
}
