import SwiftUI

struct SettingsView: View {
    @ObservedObject var model: AppModel
    @State private var confirmingUnpair = false

    var body: some View {
        List {
            Section("连接") {
                HStack {
                    Circle()
                        .fill(model.lineReady ? .green : .gray)
                        .frame(width: 10, height: 10)
                        .accessibilityHidden(true)
                    Text("线路")
                    Spacer()
                    Text(model.lineStatusLabel)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.trailing)
                }
                .accessibilityElement(children: .combine)
            }

            Section {
                Button("解除配对", role: .destructive) {
                    confirmingUnpair = true
                }
            }
        }
        .confirmationDialog(
            "解除与电脑端的配对？",
            isPresented: $confirmingUnpair,
            titleVisibility: .visible
        ) {
            Button("解除配对", role: .destructive) { model.unpair() }
            Button("取消", role: .cancel) {}
        } message: {
            Text("解除后需要重新输入配对码才能使用远程线路。")
        }
    }
}
