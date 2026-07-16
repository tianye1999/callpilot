import SwiftUI

struct CallRecordsView: View {
    @ObservedObject var model: CallHistoryModel
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            if model.records.isEmpty {
                emptyState
            } else {
                recordList
            }
        }
        .navigationTitle("记录")
        .task { await model.refresh() }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            Task { await model.refresh() }
        }
    }

    private var recordList: some View {
        List {
            Section { syncStatusRow }

            if let errorMessage = model.errorMessage {
                Section {
                    Label(errorMessage, systemImage: "exclamationmark.triangle")
                        .font(.subheadline)
                        .foregroundStyle(.primary)
                        .symbolRenderingMode(.hierarchical)
                        .accessibilityElement(children: .combine)
                }
            }

            Section {
                ForEach(model.records) { record in
                    NavigationLink(value: record.callId) {
                        CallRecordRow(record: record)
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
                            if model.isLoadingMore { ProgressView() } else { Text("加载更多") }
                            Spacer()
                        }
                    }
                    .disabled(model.isLoadingMore)
                }
            }
        }
        .navigationDestination(for: String.self) { callId in
            CallRecordDetailView(model: model, callId: callId)
        }
        .refreshable { await model.refresh() }
    }

    @ViewBuilder
    private var emptyState: some View {
        switch model.syncStatus {
        case .idle, .loading:
            ProgressView("正在同步通话记录…")
        case .live:
            ContentUnavailableView("暂无通话记录", systemImage: "clock")
                .refreshable { await model.refresh() }
        case .stale, .offline:
            ContentUnavailableView {
                Label("无法载入通话记录", systemImage: "wifi.slash")
            } description: {
                Text(model.errorMessage ?? CallHistoryCopy.unavailable)
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
        case .offline: model.errorMessage ?? CallHistoryCopy.edgeOffline
        case .idle, .loading: "正在同步"
        }
    }

    private var statusColor: Color {
        switch model.syncStatus {
        case .live: .green
        case .stale: .orange
        case .offline, .idle, .loading: .secondary
        }
    }
}

private struct CallRecordRow: View {
    let record: CallRecordItem
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: record.direction == .inbound ? "phone.arrow.down.left.fill" : "phone.arrow.up.right.fill")
                .font(.title2)
                .foregroundStyle(record.direction == .inbound ? Color.green : Color.blue)
                .frame(width: 30)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                if dynamicTypeSize.isAccessibilitySize {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(addressLabel).font(.headline)
                        Text(startedAtLabel).font(.caption).foregroundStyle(.secondary)
                    }
                } else {
                    HStack {
                        Text(addressLabel).font(.headline)
                        Spacer(minLength: 8)
                        Text(startedAtLabel).font(.caption).foregroundStyle(.secondary)
                    }
                }
                Text(metadataLabel)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                if let summaryLabel {
                    Text(summaryLabel)
                        .font(.subheadline)
                        .foregroundStyle(summaryColor)
                        .lineLimit(2)
                }
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }

    private var addressLabel: String { record.address ?? "未知号码" }

    private var startedAtLabel: String {
        Date(timeIntervalSince1970: TimeInterval(record.startedAt) / 1_000)
            .formatted(date: .abbreviated, time: .shortened)
    }

    private var metadataLabel: String {
        [statusLabel, durationLabel, sourceLabel].compactMap { $0 }.joined(separator: " · ")
    }

    private var statusLabel: String {
        switch record.status {
        case .completed: "已完成"
        case .notConnected: "未接通"
        case .failed: "失败"
        default: "状态未知"
        }
    }

    private var durationLabel: String? {
        guard let durationMs = record.durationMs else { return nil }
        let seconds = durationMs / 1_000
        return seconds >= 60 ? "\(seconds / 60)分\(seconds % 60)秒" : "\(seconds)秒"
    }

    private var sourceLabel: String? {
        switch record.source {
        case .agent: "AI 接听"
        case .remoteHandset: "手机通话"
        case .unknown: nil
        }
    }

    private var summaryLabel: String? {
        switch record.summaryState {
        case .pending: "AI 摘要生成中"
        case .ready: record.summaryPreview ?? "查看 AI 摘要"
        case .failed: "AI 摘要生成失败"
        case .unavailable: nil
        }
    }

    private var summaryColor: Color {
        record.summaryState == .failed ? .orange : .secondary
    }
}

private struct CallRecordDetailView: View {
    @ObservedObject var model: CallHistoryModel
    let callId: String
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            if let state = model.detail(for: callId), let detail = state.detail {
                detailList(detail: detail, state: state)
            } else if let state = model.detail(for: callId), state.syncStatus == .offline {
                ContentUnavailableView {
                    Label("无法载入通话详情", systemImage: "wifi.slash")
                } description: {
                    Text(state.errorMessage ?? CallHistoryCopy.unavailable)
                } actions: {
                    Button("重试") { Task { await model.refreshDetail(callId: callId) } }
                }
            } else {
                ProgressView("正在载入通话详情…")
            }
        }
        .navigationTitle("通话详情")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: callId) { await model.refreshDetail(callId: callId) }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            Task { await model.refreshDetail(callId: callId) }
        }
    }

    private func detailList(detail: CallRecordDetail, state: CallDetailState) -> some View {
        List {
            if state.syncStatus == .stale || state.errorMessage != nil {
                Section {
                    Label(
                        state.errorMessage ?? "离线缓存，内容可能不是最新",
                        systemImage: state.syncStatus == .stale ? "clock.badge.exclamationmark" : "exclamationmark.triangle"
                    )
                    .font(.subheadline)
                    .foregroundStyle(state.syncStatus == .stale ? Color.orange : Color.secondary)
                }
            }

            CallMetadataSection(record: detail.record)
            summarySection(detail: detail)

            if state.isNormalNoAIContent {
                Section("AI 对话") {
                    Label("这通电话没有 AI 对话内容", systemImage: "person.wave.2")
                        .foregroundStyle(.secondary)
                }
            } else if !state.visibleTimeline.isEmpty {
                Section("通话过程") {
                    ForEach(state.visibleTimeline) { item in
                        TimelineRow(item: item)
                    }
                }
            } else if state.syncStatus == .loading {
                Section("通话过程") { ProgressView("正在载入…") }
            } else {
                Section("通话过程") {
                    Text("暂无可显示的对话内容")
                        .foregroundStyle(.secondary)
                }
            }

            if state.hasMoreTimeline {
                Section {
                    Button {
                        Task { await model.loadMoreTimeline(callId: callId) }
                    } label: {
                        HStack {
                            Spacer()
                            if state.isLoadingMore { ProgressView() } else { Text("加载更多内容") }
                            Spacer()
                        }
                    }
                    .disabled(state.isLoadingMore)
                }
            }
        }
        .refreshable { await model.refreshDetail(callId: callId) }
    }

    @ViewBuilder
    private func summarySection(detail: CallRecordDetail) -> some View {
        switch CallSummaryPresentation(detail: detail) {
        case .hidden:
            EmptyView()
        case .pending:
            Section("AI 摘要") {
                HStack(spacing: 10) {
                    ProgressView()
                    Text("摘要生成中")
                        .foregroundStyle(.secondary)
                }
            }
        case .ready(let summary):
            Section("AI 摘要") {
                if let text = summary?.text, !text.isEmpty {
                    Text(text).textSelection(.enabled)
                }
                if let caller = summary?.callerIdentity, !caller.isEmpty {
                    LabeledContent("来电人", value: caller)
                }
                if let intent = summary?.intent, !intent.isEmpty {
                    LabeledContent("来意", value: intent)
                }
                if let urgency = summary?.urgency, !urgency.isEmpty {
                    LabeledContent("紧急程度", value: urgency)
                }
                if let callback = summary?.callbackNeeded {
                    LabeledContent("需要回电", value: callback ? "是" : "否")
                }
            }
        case .failed(let summary):
            Section("AI 摘要") {
                Label("摘要生成失败", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
                if let code = summary?.errorCode, !code.isEmpty {
                    Text("错误代码：\(code)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
        }
    }
}

private struct CallMetadataSection: View {
    let record: CallRecordItem

    var body: some View {
        Section("通话") {
            LabeledContent(record.direction == .inbound ? "来电" : "去电", value: record.address ?? "未知号码")
            LabeledContent("开始时间", value: dateLabel(record.startedAt))
            if let endedAt = record.endedAt {
                LabeledContent("结束时间", value: dateLabel(endedAt))
            }
            if let durationMs = record.durationMs {
                LabeledContent("通话时长", value: durationLabel(durationMs))
            }
            LabeledContent("结果", value: statusLabel)
            if let triageLabel {
                LabeledContent("分诊结果", value: triageLabel)
            }
        }
    }

    private var statusLabel: String {
        switch record.status {
        case .completed: "已完成"
        case .notConnected: "未接通"
        case .failed: "失败"
        default: "状态未知"
        }
    }

    private var triageLabel: String? {
        switch record.triageOutcome {
        case .aiHandled: "AI 已处理"
        case .rejected: "已礼貌拒绝"
        case .transferred: "已转接本人"
        case .unknown: "结果未知"
        case nil: nil
        }
    }

    private func dateLabel(_ milliseconds: Int64) -> String {
        Date(timeIntervalSince1970: TimeInterval(milliseconds) / 1_000)
            .formatted(date: .long, time: .standard)
    }

    private func durationLabel(_ milliseconds: Int64) -> String {
        let seconds = milliseconds / 1_000
        return seconds >= 60 ? "\(seconds / 60)分\(seconds % 60)秒" : "\(seconds)秒"
    }
}

private struct TimelineRow: View {
    let item: CallTimelineItem

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon)
                .foregroundStyle(color)
                .frame(width: 22)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline) {
                    Text(title).font(.subheadline.weight(.semibold))
                    Spacer(minLength: 8)
                    Text(timeLabel).font(.caption2).foregroundStyle(.secondary)
                }
                if let detailText {
                    Text(detailText)
                        .font(.body)
                        .foregroundStyle(.primary)
                        .textSelection(.enabled)
                }
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    private var title: String {
        switch item {
        case .transcript(let value): value.role == .caller ? "对方" : "AI"
        case .result: "通话结果"
        case .triage: "智能分诊"
        case .takeover: "转接状态"
        case .unknown: ""
        }
    }

    private var detailText: String? {
        switch item {
        case .transcript(let value): value.text
        case .result(let value): value.summary ?? statusLabel(value.status)
        case .triage(let value): "\(categoryLabel(value.category)) · \(actionLabel(value.action))"
        case .takeover(let value): takeoverLabel(value.state)
        case .unknown: nil
        }
    }

    private var icon: String {
        switch item {
        case .transcript(let value): value.role == .caller ? "person.fill" : "sparkles"
        case .result: "checkmark.circle"
        case .triage: "arrow.triangle.branch"
        case .takeover: "iphone.and.arrow.forward"
        case .unknown: "circle"
        }
    }

    private var color: Color {
        switch item {
        case .transcript(let value): value.role == .caller ? .primary : .blue
        case .result: .green
        case .triage: .purple
        case .takeover: .orange
        case .unknown: .secondary
        }
    }

    private var timeLabel: String {
        Date(timeIntervalSince1970: TimeInterval(item.occurredAt) / 1_000)
            .formatted(date: .omitted, time: .shortened)
    }

    private func statusLabel(_ status: CallRecordStatus) -> String {
        switch status {
        case .completed: "已完成"
        case .notConnected: "未接通"
        case .failed: "失败"
        default: "状态未知"
        }
    }

    private func categoryLabel(_ category: TriageCategory) -> String {
        switch category {
        case .marketing: "营销来电"
        case .personal: "个人来电"
        case .needsOwner: "需要本人处理"
        case .unknown: "类型未知"
        }
    }

    private func actionLabel(_ action: TriageAction) -> String {
        switch action {
        case .clarify: "继续确认"
        case .continueAI: "AI 继续处理"
        case .reject: "已拒绝"
        case .transfer: "已转接"
        }
    }

    private func takeoverLabel(_ state: TakeoverState) -> String {
        switch state {
        case .requested: "正在请求转接"
        case .committed: "已由本人接听"
        case .ownerHangup: "本人已挂断"
        case .failed: "转接失败"
        }
    }
}
