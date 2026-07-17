import SwiftUI

/// 拨号页(对齐 Android DialScreen)。12 键盘 + 拨号键。
struct DialView: View {
    @ObservedObject var model: AppModel
    @State private var number = ""

    private let keys = ["1","2","3","4","5","6","7","8","9","*","0","#"]

    var body: some View {
        VStack(spacing: 16) {
            Text(number.isEmpty ? " " : number)
                .font(.system(size: 34, weight: .medium, design: .rounded))
                .frame(maxWidth: .infinity, minHeight: 56)

            LazyVGrid(columns: Array(repeating: GridItem(.flexible()), count: 3), spacing: 18) {
                ForEach(keys, id: \.self) { k in
                    Button(k) { if number.count < 32 { number += k } }
                        .font(.system(size: 30))
                        .frame(width: 74, height: 74)
                        .background(Color.gray.opacity(0.12), in: Circle())
                        .foregroundStyle(.primary)
                }
            }

            // 对齐 Apple 电话:拨打键恒居中列,删除键在右列出现/消失(占位隐藏),不挤动拨打键。
            LazyVGrid(columns: Array(repeating: GridItem(.flexible()), count: 3), spacing: 18) {
                Color.clear.frame(width: 74, height: 74)
                    .accessibilityHidden(true)

                Button {
                    Task { await model.startCall(number: number) }
                } label: {
                    Image(systemName: "phone.fill").font(.title)
                        .frame(width: 70, height: 70)
                        .background(model.lineReady && !number.isEmpty ? .green : .gray, in: Circle())
                        .foregroundStyle(.white)
                }
                .disabled(!model.lineReady || number.isEmpty)
                .accessibilityLabel(L10n.text("dial.accessibility.call"))

                Button { number.removeLast() } label: {
                    Image(systemName: "delete.left").font(.title2)
                        .frame(width: 74, height: 74)
                }
                .opacity(number.isEmpty ? 0 : 1)
                .disabled(number.isEmpty)
                .accessibilityLabel(L10n.text("dial.accessibility.delete"))
            }
            Spacer()
        }
        .padding(24)
    }
}
