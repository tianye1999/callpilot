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
        .navigationTitle(L10n.text("tab.records"))
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
        .navigationDestination(for: String.self) { callId in
            CallRecordDetailView(model: model, callId: callId)
        }
        .refreshable { await model.refresh() }
    }

    @ViewBuilder
    private var emptyState: some View {
        switch model.syncStatus {
        case .idle, .loading:
            ProgressView(L10n.text("calls.loading"))
        case .live:
            ContentUnavailableView(L10n.text("calls.empty"), systemImage: "clock")
                .refreshable { await model.refresh() }
        case .stale, .offline:
            ContentUnavailableView {
                Label(L10n.text("calls.load_failed"), systemImage: "wifi.slash")
            } description: {
                Text(model.errorMessage ?? CallHistoryCopy.unavailable)
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
        case .offline: model.errorMessage ?? CallHistoryCopy.edgeOffline
        case .idle, .loading: L10n.text("common.syncing")
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

struct CallRecordRow: View {
    let record: CallRecordItem
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Image(systemName: record.direction == .inbound ? "phone.arrow.down.left.fill" : "phone.arrow.up.right.fill")
                .font(.system(size: 22))
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

    private var addressLabel: String {
        record.address ?? L10n.text("calls.unknown_address")
    }

    private var startedAtLabel: String {
        Date(timeIntervalSince1970: TimeInterval(record.startedAt) / 1_000)
            .formatted(date: .abbreviated, time: .shortened)
    }

    private var metadataLabel: String {
        [statusLabel, durationLabel, sourceLabel].compactMap { $0 }.joined(separator: " · ")
    }

    private var statusLabel: String {
        CallRecordCopy.status(record.status)
    }

    private var durationLabel: String? {
        guard let durationMs = record.durationMs else { return nil }
        return CallRecordCopy.duration(durationMs)
    }

    private var sourceLabel: String? {
        CallRecordCopy.source(record.source)
    }

    private var summaryLabel: String? {
        switch record.summaryState {
        case .pending: L10n.text("calls.summary.pending_preview")
        case .ready: record.summaryPreview ?? L10n.text("calls.summary.ready_preview")
        case .failed: L10n.text("calls.summary.failed_preview")
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
                    Label(L10n.text("calls.detail.load_failed"), systemImage: "wifi.slash")
                } description: {
                    Text(state.errorMessage ?? CallHistoryCopy.unavailable)
                } actions: {
                    Button(L10n.text("common.retry")) {
                        Task { await model.refreshDetail(callId: callId) }
                    }
                }
            } else {
                ProgressView(L10n.text("calls.detail.loading"))
            }
        }
        .navigationTitle(L10n.text("calls.detail.title"))
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
                        state.errorMessage ?? L10n.text("common.stale_cache"),
                        systemImage: state.syncStatus == .stale ? "clock.badge.exclamationmark" : "exclamationmark.triangle"
                    )
                    .font(.subheadline)
                    .foregroundStyle(state.syncStatus == .stale ? Color.orange : Color.secondary)
                }
            }

            CallMetadataSection(record: detail.record)
            summarySection(detail: detail)

            if state.isNormalNoAIContent {
                Section(L10n.text("calls.ai.section")) {
                    Label(L10n.text("calls.ai.no_content"), systemImage: "person.wave.2")
                        .foregroundStyle(.secondary)
                }
            } else if !state.visibleTimeline.isEmpty {
                Section(L10n.text("calls.timeline.section")) {
                    ForEach(state.visibleTimeline) { item in
                        TimelineRow(item: item)
                    }
                }
            } else if state.syncStatus == .loading {
                Section(L10n.text("calls.timeline.section")) {
                    ProgressView(L10n.text("calls.timeline.loading"))
                }
            } else {
                Section(L10n.text("calls.timeline.section")) {
                    Text(L10n.text("calls.timeline.empty"))
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
                            if state.isLoadingMore {
                                ProgressView()
                            } else {
                                Text(L10n.text("calls.timeline.load_more"))
                            }
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
            Section(L10n.text("calls.summary.section")) {
                HStack(spacing: 10) {
                    ProgressView()
                    Text(L10n.text("calls.summary.loading"))
                        .foregroundStyle(.secondary)
                }
            }
        case .ready(let summary):
            Section(L10n.text("calls.summary.section")) {
                if let text = summary?.text, !text.isEmpty {
                    Text(text).textSelection(.enabled)
                }
                if let caller = summary?.callerIdentity, !caller.isEmpty {
                    LabeledContent(L10n.text("calls.summary.caller"), value: caller)
                }
                if let intent = summary?.intent, !intent.isEmpty {
                    LabeledContent(L10n.text("calls.summary.intent"), value: intent)
                }
                if let urgency = summary?.urgency, !urgency.isEmpty {
                    LabeledContent(L10n.text("calls.summary.urgency"), value: urgency)
                }
                if let callback = summary?.callbackNeeded {
                    LabeledContent(
                        L10n.text("calls.summary.callback"),
                        value: callback ? L10n.text("common.yes") : L10n.text("common.no")
                    )
                }
            }
        case .failed(let summary):
            Section(L10n.text("calls.summary.section")) {
                Label(L10n.text("calls.summary.failed"), systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
                if let code = summary?.errorCode, !code.isEmpty {
                    Text(L10n.format("calls.summary.error_code", code))
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
        Section(L10n.text("calls.metadata.section")) {
            LabeledContent(
                record.direction == .inbound
                    ? L10n.text("calls.direction.inbound")
                    : L10n.text("calls.direction.outbound"),
                value: record.address ?? L10n.text("calls.unknown_address")
            )
            LabeledContent(L10n.text("calls.metadata.started_at"), value: dateLabel(record.startedAt))
            if let endedAt = record.endedAt {
                LabeledContent(L10n.text("calls.metadata.ended_at"), value: dateLabel(endedAt))
            }
            if let durationMs = record.durationMs {
                LabeledContent(L10n.text("calls.metadata.duration"), value: durationLabel(durationMs))
            }
            LabeledContent(L10n.text("calls.metadata.result"), value: statusLabel)
            if let triageLabel {
                LabeledContent(L10n.text("calls.metadata.triage"), value: triageLabel)
            }
        }
    }

    private var statusLabel: String {
        CallRecordCopy.status(record.status)
    }

    private var triageLabel: String? {
        CallRecordCopy.triageOutcome(record.triageOutcome)
    }

    private func dateLabel(_ milliseconds: Int64) -> String {
        Date(timeIntervalSince1970: TimeInterval(milliseconds) / 1_000)
            .formatted(date: .long, time: .standard)
    }

    private func durationLabel(_ milliseconds: Int64) -> String {
        CallRecordCopy.duration(milliseconds)
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
        case .transcript(let value):
            value.role == .caller
                ? L10n.text("calls.timeline.caller")
                : L10n.text("calls.timeline.ai")
        case .result: L10n.text("calls.timeline.result")
        case .triage: L10n.text("calls.timeline.triage")
        case .takeover: L10n.text("calls.timeline.takeover")
        case .unknown: ""
        }
    }

    private var detailText: String? {
        switch item {
        case .transcript(let value): value.text
        case .result(let value): value.summary ?? statusLabel(value.status)
        case .triage(let value):
            "\(CallRecordCopy.category(value.category)) · \(CallRecordCopy.action(value.action))"
        case .takeover(let value): CallRecordCopy.takeover(value.state)
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
        CallRecordCopy.status(status)
    }
}

private enum CallRecordCopy {
    static func status(_ status: CallRecordStatus) -> String {
        switch status {
        case .completed: L10n.text("calls.status.completed")
        case .notConnected: L10n.text("calls.status.not_connected")
        case .failed: L10n.text("calls.status.failed")
        default: L10n.text("calls.status.unknown")
        }
    }

    static func duration(_ milliseconds: Int64) -> String {
        let seconds = milliseconds / 1_000
        if seconds >= 60 {
            return L10n.format("calls.duration.minutes_seconds", seconds / 60, seconds % 60)
        }
        return L10n.format("calls.duration.seconds", seconds)
    }

    static func source(_ source: CallSource) -> String? {
        switch source {
        case .agent: L10n.text("calls.source.agent")
        case .remoteHandset: L10n.text("calls.source.remote_handset")
        case .unknown: nil
        }
    }

    static func triageOutcome(_ outcome: CallTriageOutcome?) -> String? {
        switch outcome {
        case .aiHandled: L10n.text("calls.triage_outcome.ai_handled")
        case .rejected: L10n.text("calls.triage_outcome.rejected")
        case .transferred: L10n.text("calls.triage_outcome.transferred")
        case .unknown: L10n.text("calls.triage_outcome.unknown")
        case nil: nil
        }
    }

    static func category(_ category: TriageCategory) -> String {
        switch category {
        case .marketing: L10n.text("calls.triage_category.marketing")
        case .personal: L10n.text("calls.triage_category.personal")
        case .needsOwner: L10n.text("calls.triage_category.needs_owner")
        case .unknown: L10n.text("calls.triage_category.unknown")
        }
    }

    static func action(_ action: TriageAction) -> String {
        switch action {
        case .clarify: L10n.text("calls.triage_action.clarify")
        case .continueAI: L10n.text("calls.triage_action.continue_ai")
        case .reject: L10n.text("calls.triage_action.reject")
        case .transfer: L10n.text("calls.triage_action.transfer")
        }
    }

    static func takeover(_ state: TakeoverState) -> String {
        switch state {
        case .requested: L10n.text("calls.takeover.requested")
        case .committed: L10n.text("calls.takeover.committed")
        case .ownerHangup: L10n.text("calls.takeover.owner_hangup")
        case .failed: L10n.text("calls.takeover.failed")
        }
    }
}
