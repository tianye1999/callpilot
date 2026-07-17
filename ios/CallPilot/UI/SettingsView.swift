import SwiftUI

struct SettingsView: View {
    @ObservedObject var model: AppModel
    @State private var confirmingClear = false
    @State private var confirmingUnpair = false

    var body: some View {
        List {
            Section(L10n.text("settings.connection.section")) {
                DeviceStatusRow(
                    title: L10n.text("settings.connection.edge"),
                    value: edgeStatusText,
                    color: edgeStatusColor
                )
                DeviceStatusRow(
                    title: L10n.text("settings.connection.sim"),
                    value: simStatusText,
                    color: simStatusColor
                )
                HStack {
                    Label(deviceSyncText, systemImage: deviceSyncIcon)
                        .foregroundStyle(.secondary)
                    Spacer()
                    if model.deviceStatusRefreshing { ProgressView() }
                }
                Button {
                    Task { await model.refreshDeviceStatus() }
                } label: {
                    Label(
                        L10n.text("settings.connection.refresh"),
                        systemImage: "arrow.clockwise"
                    )
                }
                .disabled(model.deviceStatusRefreshing)
            }

            Section {
                if let inbox = model.messageInbox {
                    MessageCacheSettingsRow(model: inbox)
                }
                if let history = model.callHistory {
                    CallCacheSettingsRow(model: history)
                }
                Button(L10n.text("settings.cache.clear"), role: .destructive) {
                    confirmingClear = true
                }
            } header: {
                Text(L10n.text("settings.cache.section"))
            } footer: {
                Text(L10n.text("settings.cache.footer"))
            }

            Section(L10n.text("settings.privacy.section")) {
                Text(L10n.text("settings.privacy.relay"))
                Text(L10n.text("settings.privacy.local"))
                Link(destination: AppLinks.privacyPolicy) {
                    Label(
                        L10n.text("settings.legal.privacy_policy"),
                        systemImage: "hand.raised"
                    )
                }
                Link(destination: AppLinks.terms) {
                    Label(
                        L10n.text("settings.legal.terms"),
                        systemImage: "doc.text"
                    )
                }
                Link(destination: AppLinks.support) {
                    Label(
                        L10n.text("settings.legal.support"),
                        systemImage: "questionmark.circle"
                    )
                }
            }

            Section {
                Button(L10n.text("settings.unpair.action"), role: .destructive) {
                    confirmingUnpair = true
                }
            }
        }
        .task {
            model.loadCachedContentForSettings()
            await model.refreshDeviceStatus()
        }
        .refreshable { await model.refreshDeviceStatus() }
        .confirmationDialog(
            L10n.text("settings.cache.clear.confirm_title"),
            isPresented: $confirmingClear,
            titleVisibility: .visible
        ) {
            Button(L10n.text("settings.cache.clear.confirm_action"), role: .destructive) {
                model.clearLocalContent()
            }
            Button(L10n.text("common.cancel"), role: .cancel) {}
        } message: {
            Text(L10n.text("settings.cache.clear.confirm_message"))
        }
        .confirmationDialog(
            L10n.text("settings.unpair.confirm_title"),
            isPresented: $confirmingUnpair,
            titleVisibility: .visible
        ) {
            Button(L10n.text("settings.unpair.action"), role: .destructive) { model.unpair() }
            Button(L10n.text("common.cancel"), role: .cancel) {}
        } message: {
            Text(L10n.text("settings.unpair.confirm_message"))
        }
    }

    private var edgeStatusText: String {
        guard let status = model.deviceStatus else {
            return model.deviceStatusSync == .loading
                ? L10n.text("settings.status.checking")
                : L10n.text("settings.status.unavailable")
        }
        return status.connected
            ? L10n.text("settings.status.online")
            : L10n.text("settings.status.offline")
    }

    private var edgeStatusColor: Color {
        guard let status = model.deviceStatus else { return .gray }
        return status.connected ? .green : .red
    }

    private var simStatusText: String {
        guard let status = model.deviceStatus, status.connected else {
            return model.deviceStatusSync == .loading
                ? L10n.text("settings.status.checking")
                : L10n.text("settings.status.unavailable")
        }
        return status.modemOnline
            ? L10n.text("settings.status.online")
            : L10n.text("settings.status.offline")
    }

    private var simStatusColor: Color {
        guard let status = model.deviceStatus, status.connected else { return .gray }
        return status.modemOnline ? .green : .red
    }

    private var deviceSyncText: String {
        switch model.deviceStatusSync {
        case .idle: L10n.text("settings.sync.idle")
        case .loading: L10n.text("settings.sync.loading")
        case .live: L10n.text("settings.sync.live")
        case .stale: L10n.text("settings.sync.stale")
        case .offline: L10n.text("settings.sync.offline")
        }
    }

    private var deviceSyncIcon: String {
        switch model.deviceStatusSync {
        case .live: "checkmark.circle"
        case .stale: "clock.badge.exclamationmark"
        case .offline: "wifi.slash"
        case .idle, .loading: "arrow.triangle.2.circlepath"
        }
    }
}

private struct DeviceStatusRow: View {
    let title: String
    let value: String
    let color: Color

    var body: some View {
        LabeledContent {
            HStack(spacing: 7) {
                Circle()
                    .fill(color)
                    .frame(width: 9, height: 9)
                    .accessibilityHidden(true)
                Text(value).foregroundStyle(.secondary)
            }
        } label: {
            Text(title)
        }
        .accessibilityElement(children: .combine)
    }
}

private struct MessageCacheSettingsRow: View {
    @ObservedObject var model: MessageInboxModel

    var body: some View {
        CacheStatusRow(
            title: L10n.text("settings.cache.messages"),
            count: L10n.format("settings.cache.messages_count", model.messages.count),
            state: cacheStateText(model.syncStatus)
        )
    }

    private func cacheStateText(_ state: MessageSyncStatus) -> String {
        switch state {
        case .idle: L10n.text("settings.cache.empty")
        case .loading: L10n.text("settings.cache.loading")
        case .live: L10n.text("settings.cache.live")
        case .stale: L10n.text("settings.cache.stale")
        case .offline: L10n.text("settings.cache.offline")
        }
    }
}

private struct CallCacheSettingsRow: View {
    @ObservedObject var model: CallHistoryModel

    var body: some View {
        CacheStatusRow(
            title: L10n.text("settings.cache.calls"),
            count: L10n.format("settings.cache.calls_count", model.records.count),
            state: cacheStateText(model.syncStatus)
        )
    }

    private func cacheStateText(_ state: CallHistorySyncStatus) -> String {
        switch state {
        case .idle: L10n.text("settings.cache.empty")
        case .loading: L10n.text("settings.cache.loading")
        case .live: L10n.text("settings.cache.live")
        case .stale: L10n.text("settings.cache.stale")
        case .offline: L10n.text("settings.cache.offline")
        }
    }
}

private struct CacheStatusRow: View {
    let title: String
    let count: String
    let state: String

    var body: some View {
        LabeledContent {
            VStack(alignment: .trailing, spacing: 2) {
                Text(count)
                Text(state)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } label: {
            Text(title)
        }
        .accessibilityElement(children: .combine)
    }
}
