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

            HStack(spacing: 30) {
                Button {
                    Task { await model.startCall(number: number) }
                } label: {
                    Image(systemName: "phone.fill").font(.title)
                        .frame(width: 70, height: 70)
                        .background(model.lineReady && !number.isEmpty ? .green : .gray, in: Circle())
                        .foregroundStyle(.white)
                }
                .disabled(!model.lineReady || number.isEmpty)

                if !number.isEmpty {
                    Button { number.removeLast() } label: {
                        Image(systemName: "delete.left").font(.title2)
                    }
                }
            }
            Spacer()
        }
        .padding(24)
    }
}
