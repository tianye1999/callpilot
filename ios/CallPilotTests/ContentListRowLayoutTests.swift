import SwiftUI
import XCTest
@testable import CallPilot

@MainActor
final class ContentListRowLayoutTests: XCTestCase {
    private let largeSizes: [DynamicTypeSize] = [
        .large,
        .xLarge,
        .xxLarge,
        .xxxLarge,
        .accessibility1,
        .accessibility2,
        .accessibility3,
        .accessibility4,
        .accessibility5,
    ]

    func testMessageLeadingIconStaysBoundedAndFirstLineAlignedAtLargeSizes() throws {
        for size in largeSizes {
            let image = try render(
                MessageRow(message: Self.message)
                    .environment(\.dynamicTypeSize, size),
                captureName: "messages-\(size)"
            )
            let bounds = try coloredPixelBounds(in: image)
            XCTAssertLessThanOrEqual(bounds.width, 30, "message icon grew at \(size)")
            XCTAssertLessThanOrEqual(bounds.height, 30, "message icon grew at \(size)")
            let firstLine = try firstTextLineBounds(in: image)
            XCTAssertEqual(
                bounds.midY,
                firstLine.midY,
                accuracy: 10,
                "message icon drifted away from the first headline line at \(size)"
            )
        }
    }

    func testCallLeadingIconStaysBoundedAndFirstLineAlignedAtLargeSizes() throws {
        for size in largeSizes {
            let image = try render(
                CallRecordRow(record: Self.record)
                    .environment(\.dynamicTypeSize, size),
                captureName: "calls-\(size)"
            )
            let bounds = try coloredPixelBounds(in: image)
            XCTAssertLessThanOrEqual(bounds.width, 30, "call icon grew at \(size)")
            XCTAssertLessThanOrEqual(bounds.height, 30, "call icon grew at \(size)")
            let firstLine = try firstTextLineBounds(in: image)
            XCTAssertEqual(
                bounds.midY,
                firstLine.midY,
                accuracy: 10,
                "call icon drifted away from the first headline line at \(size)"
            )
        }
    }

    private func render<V: View>(_ view: V, captureName: String) throws -> UIImage {
        let content = view
            .frame(width: 330, alignment: .leading)
            .padding(16)
            .background(Color.white)
            .fixedSize(horizontal: false, vertical: true)
        let renderer = ImageRenderer(content: content)
        renderer.scale = 3
        guard let image = renderer.uiImage else {
            throw XCTSkip("SwiftUI ImageRenderer is unavailable")
        }
        if let directory = ProcessInfo.processInfo.environment["CALLPILOT_ROW_CAPTURE_DIR"]
            ?? (FileManager.default.fileExists(atPath: "/tmp/callpilot-icon-alignment/capture-enabled")
                ? "/tmp/callpilot-icon-alignment/current" : nil) {
            let url = URL(fileURLWithPath: directory, isDirectory: true)
                .appendingPathComponent("\(captureName).png")
            try FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try image.pngData()?.write(to: url)
        }
        return image
    }

    private func coloredPixelBounds(in image: UIImage) throws -> CGRect {
        let bitmap = try bitmap(in: image)
        var minX = bitmap.width
        var minY = bitmap.height
        var maxX = -1
        var maxY = -1

        bitmap.forEachPixel { x, y, r, g, b in
            if g > r + 25, g > b + 25, g > 70 {
                minX = min(minX, x)
                minY = min(minY, y)
                maxX = max(maxX, x)
                maxY = max(maxY, y)
            }
        }
        guard maxX >= minX, maxY >= minY else {
            XCTFail("No leading icon pixels found")
            return .zero
        }
        return bitmap.pointRect(minX: minX, minY: minY, maxX: maxX, maxY: maxY)
    }

    private func firstTextLineBounds(in image: UIImage) throws -> CGRect {
        let bitmap = try bitmap(in: image)
        var activeRows: [Int] = []
        for y in 0..<bitmap.height {
            var darkPixels = 0
            bitmap.forEachPixel(inRow: y) { x, r, g, b in
                if x > 150, r < 70, g < 70, b < 70 { darkPixels += 1 }
            }
            if darkPixels >= 3 { activeRows.append(y) }
        }
        guard let first = activeRows.first else {
            XCTFail("No headline pixels found")
            return .zero
        }
        var last = first
        for row in activeRows.dropFirst() {
            if row > last + 2 { break }
            last = row
        }
        return CGRect(
            x: 0,
            y: CGFloat(first) / 3,
            width: 0,
            height: CGFloat(last - first + 1) / 3
        )
    }

    private func bitmap(in image: UIImage) throws -> RGBABitmap {
        guard let source = image.cgImage else {
            throw XCTSkip("Rendered image has no CGImage")
        }
        let width = source.width
        let height = source.height
        let bytesPerRow = width * 4
        var pixels = [UInt8](repeating: 0, count: bytesPerRow * height)
        guard let context = CGContext(
            data: &pixels,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: bytesPerRow,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            throw XCTSkip("Unable to allocate bitmap context")
        }
        context.draw(source, in: CGRect(x: 0, y: 0, width: width, height: height))
        return RGBABitmap(width: width, height: height, bytesPerRow: bytesPerRow, pixels: pixels)
    }

    private static let message = SMSMessage(
        messageId: "msg_fixture_layout_0001",
        revision: "revision_layout_message_0001",
        direction: .inbound,
        address: "+15550100001",
        text: "A deliberately long synthetic message that wraps across multiple lines.",
        occurredAt: 1_784_160_001_000,
        recordedAt: 1_784_160_002_000,
        status: .received
    )

    private static let record = CallRecordItem(
        callId: "call_fixture_layout_0001",
        revision: "revision_layout_call_0001",
        direction: .inbound,
        address: "+15550100002",
        startedAt: 1_784_161_000_000,
        endedAt: 1_784_161_120_000,
        durationMs: 120_000,
        status: .completed,
        answered: true,
        source: .agent,
        summaryState: .ready,
        summaryPreview: "A synthetic summary that wraps across multiple lines.",
        hasTranscript: true,
        triageOutcome: .transferred
    )
}

private struct RGBABitmap {
    let width: Int
    let height: Int
    let bytesPerRow: Int
    let pixels: [UInt8]

    func forEachPixel(_ body: (Int, Int, Int, Int, Int) -> Void) {
        for y in 0..<height {
            forEachPixel(inRow: y) { x, r, g, b in body(x, y, r, g, b) }
        }
    }

    func forEachPixel(inRow y: Int, _ body: (Int, Int, Int, Int) -> Void) {
        for x in 0..<width {
            let offset = y * bytesPerRow + x * 4
            body(x, Int(pixels[offset]), Int(pixels[offset + 1]), Int(pixels[offset + 2]))
        }
    }

    func pointRect(minX: Int, minY: Int, maxX: Int, maxY: Int) -> CGRect {
        CGRect(
            x: CGFloat(minX) / 3,
            y: CGFloat(minY) / 3,
            width: CGFloat(maxX - minX + 1) / 3,
            height: CGFloat(maxY - minY + 1) / 3
        )
    }
}
