import Foundation

enum ContentTestFixtures {
    static func data(named name: String) throws -> Data {
        let repositoryRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        return try Data(
            contentsOf: repositoryRoot
                .appendingPathComponent("docs/fixtures/content-sync/v1", isDirectory: true)
                .appendingPathComponent(name)
        )
    }
}
