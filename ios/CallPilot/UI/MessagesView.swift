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
        .navigationTitle("短信")
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
                                Text("加载更多")
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
            ProgressView("正在同步短信…")
        case .live:
            ContentUnavailableView("暂无短信", systemImage: "message")
                .refreshable { await model.refresh() }
        case .stale, .offline:
            ContentUnavailableView {
                Label("无法载入短信", systemImage: "wifi.slash")
            } description: {
                Text(model.errorMessage ?? MessageInboxCopy.unavailable)
            } actions: {
                Button("重试") { Task { await model.refresh() } }
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
        case .live: "已同步"
        case .stale: "离线缓存，内容可能不是最新"
        case .offline: model.errorMessage ?? MessageInboxCopy.edgeOffline
        case .idle, .loading: "正在同步"
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
                Text(message.direction == .inbound ? "收到" : deliveryLabel)
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
        case .sent: "已发送"
        case .failed: "发送失败"
        case .error: "发送异常"
        case .received: "收到"
        }
    }
}

private struct MessageDetailView: View {
    let message: SMSMessage

    var body: some View {
        List {
            Section("联系人") {
                LabeledContent(message.direction == .inbound ? "发件人" : "收件人", value: message.address)
                LabeledContent("时间", value: messageDate)
                LabeledContent("状态", value: statusLabel)
            }
            Section("内容") {
                Text(message.text)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .navigationTitle("短信详情")
        .navigationBarTitleDisplayMode(.inline)
    }

    private var messageDate: String {
        Date(timeIntervalSince1970: TimeInterval(message.occurredAt) / 1_000)
            .formatted(date: .long, time: .standard)
    }

    private var statusLabel: String {
        switch message.status {
        case .received: "已收到"
        case .sent: "已发送"
        case .failed: "发送失败"
        case .error: "发送异常"
        }
    }
}
