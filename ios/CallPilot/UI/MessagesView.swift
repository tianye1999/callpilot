import SwiftUI

struct MessagesView: View {
    @ObservedObject var model: MessageInboxModel
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            if model.messages.isEmpty {
                emptyState
            } else {
                messageList
            }
        }
        .navigationTitle(L10n.text("tab.messages"))
        .task {
            await model.refresh()
            await markDisplayedAfterRender()
        }
        .onAppear {
            model.setVisible(true)
            Task { await markDisplayedAfterRender() }
        }
        .onDisappear { model.setVisible(false) }
        .onChange(of: model.collectionRevision) { _, _ in
            Task { await markDisplayedAfterRender() }
        }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            Task {
                await model.refresh()
                await markDisplayedAfterRender()
            }
        }
    }

    private var messageList: some View {
        List {
            Section {
                syncStatusRow
            }

            if let errorMessage = model.errorMessage {
                Section {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                        Text(errorMessage)
                            .font(.subheadline)
                            .foregroundStyle(.primary)
                    }
                    .accessibilityElement(children: .combine)
                }
            }

            Section {
                ForEach(model.messages) { message in
                    NavigationLink(value: message) {
                        MessageRow(message: message)
                    }
                }
            }

            if model.hasMore {
                Section {
                    Button {
                        Task { await model.loadMore() }
                    } label: {
                        HStack {
                            Spacer()
                            if model.isLoadingMore {
                                ProgressView()
                            } else {
                                Text(L10n.text("common.load_more"))
                            }
                            Spacer()
                        }
                    }
                    .disabled(model.isLoadingMore)
                }
            }
        }
        .navigationDestination(for: SMSMessage.self) { message in
            MessageDetailView(message: message)
        }
        .refreshable {
            await model.refresh()
            await markDisplayedAfterRender()
        }
    }

    @ViewBuilder
    private var emptyState: some View {
        switch model.syncStatus {
        case .idle, .loading:
            ProgressView(L10n.text("messages.loading"))
        case .live:
            ContentUnavailableView(L10n.text("messages.empty"), systemImage: "message")
                .refreshable { await model.refresh() }
        case .stale, .offline:
            ContentUnavailableView {
                Label(L10n.text("messages.load_failed"), systemImage: "wifi.slash")
            } description: {
                Text(model.errorMessage ?? MessageInboxCopy.unavailable)
            } actions: {
                Button(L10n.text("common.retry")) { Task { await model.refresh() } }
            }
        }
    }

    private var syncStatusRow: some View {
        HStack(spacing: 8) {
            Image(systemName: statusIcon)
                .foregroundStyle(statusColor)
            Text(statusText)
                .font(.subheadline)
                .foregroundStyle(.primary)
            Spacer()
            if model.isRefreshing { ProgressView() }
        }
        .accessibilityElement(children: .combine)
    }

    private var statusIcon: String {
        switch model.syncStatus {
        case .live: "checkmark.circle.fill"
        case .stale: "clock.badge.exclamationmark"
        case .offline: "wifi.slash"
        case .idle, .loading: "arrow.triangle.2.circlepath"
        }
    }

    private var statusText: String {
        switch model.syncStatus {
        case .live: L10n.text("common.synced")
        case .stale: L10n.text("common.stale_cache")
        case .offline: model.errorMessage ?? MessageInboxCopy.edgeOffline
        case .idle, .loading: L10n.text("common.syncing")
        }
    }

    private var statusColor: Color {
        switch model.syncStatus {
        case .live: .green
        case .stale: .orange
        case .offline: .secondary
        case .idle, .loading: .secondary
        }
    }

    private func markDisplayedAfterRender() async {
        await Task.yield()
        model.markLatestDisplayed()
    }
}

private struct MessageRow: View {
    let message: SMSMessage
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: message.direction == .inbound ? "arrow.down.left.circle.fill" : "arrow.up.right.circle.fill")
                .font(.title2)
                .foregroundStyle(message.direction == .inbound ? Color.green : Color.blue)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                if dynamicTypeSize.isAccessibilitySize {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(message.address)
                            .font(.headline)
                        Text(messageDate)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                } else {
                    HStack {
                        Text(message.address)
                            .font(.headline)
                        Spacer(minLength: 8)
                        Text(messageDate)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Text(message.text)
                    .font(.body)
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                Text(
                    message.direction == .inbound
                        ? L10n.text("messages.status.received_short")
                        : deliveryLabel
                )
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }

    private var messageDate: String {
        Date(timeIntervalSince1970: TimeInterval(message.occurredAt) / 1_000)
            .formatted(date: .abbreviated, time: .shortened)
    }

    private var deliveryLabel: String {
        switch message.status {
        case .sent: L10n.text("messages.status.sent")
        case .failed: L10n.text("messages.status.failed")
        case .error: L10n.text("messages.status.error")
        case .received: L10n.text("messages.status.received_short")
        }
    }
}

private struct MessageDetailView: View {
    let message: SMSMessage

    var body: some View {
        List {
            Section(L10n.text("messages.detail.contact_section")) {
                LabeledContent(
                    message.direction == .inbound
                        ? L10n.text("messages.detail.sender")
                        : L10n.text("messages.detail.recipient"),
                    value: message.address
                )
                LabeledContent(L10n.text("messages.detail.time"), value: messageDate)
                LabeledContent(L10n.text("messages.detail.status"), value: statusLabel)
            }
            Section(L10n.text("messages.detail.content_section")) {
                Text(message.text)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .navigationTitle(L10n.text("messages.detail.title"))
        .navigationBarTitleDisplayMode(.inline)
    }

    private var messageDate: String {
        Date(timeIntervalSince1970: TimeInterval(message.occurredAt) / 1_000)
            .formatted(date: .long, time: .standard)
    }

    private var statusLabel: String {
        switch message.status {
        case .received: L10n.text("messages.status.received")
        case .sent: L10n.text("messages.status.sent")
        case .failed: L10n.text("messages.status.failed")
        case .error: L10n.text("messages.status.error")
        }
    }
}
