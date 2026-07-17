package ai.bondings.callpilot.protocol

import java.io.File

internal object ContentTestFixtures {
    fun text(name: String): String {
        val root = File(System.getProperty("user.dir") ?: ".")
        val candidates = generateSequence(root) { it.parentFile }
            .take(6)
            .map { File(it, "docs/fixtures/content-sync/v1/$name") }
        return candidates.firstOrNull(File::isFile)?.readText()
            ?: error("Missing shared content fixture: $name")
    }
}
