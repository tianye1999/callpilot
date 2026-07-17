package ai.bondings.callpilot.content

import ai.bondings.callpilot.protocol.CallRecordDetail
import ai.bondings.callpilot.protocol.CallRecordsPage
import ai.bondings.callpilot.protocol.CallTimelinePage
import ai.bondings.callpilot.protocol.ContentTestFixtures
import java.io.File
import java.nio.file.Files
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CallHistoryCacheTest {
    @Test
    fun `encrypted history round trips details and timeline only for same device`() {
        val directory = Files.createTempDirectory("callpilot-call-history-test").toFile()
        try {
            val file = File(directory, "call-history-v1.bin")
            val protected = ProtectedJsonStore(file, CallHistoryTestCipher())
            val store = CallHistoryCacheStore(protected)
            val snapshot = fixtureSnapshot()

            store.save(snapshot)

            assertFalse(file.readText().contains("Synthetic caller requested"))
            assertEquals(snapshot, store.load(DEVICE_ID))
            assertNull(store.load("device_otherdevice12"))
            assertFalse(file.exists())
        } finally {
            directory.deleteRecursively()
        }
    }

    @Test
    fun `authenticated cache with mismatched detail identity fails closed`() {
        val directory = Files.createTempDirectory("callpilot-call-history-test").toFile()
        try {
            val file = File(directory, "call-history-v1.bin")
            val protected = ProtectedJsonStore(file, CallHistoryTestCipher())
            val store = CallHistoryCacheStore(protected)
            val snapshot = fixtureSnapshot()
            val cached = snapshot.details.values.single()
            val invalid = snapshot.copy(details = mapOf("call_fixture_wrong_0001" to cached))
            protected.write(DEVICE_ID, Json.encodeToString(invalid).encodeToByteArray())

            assertNull(store.load(DEVICE_ID))
            assertTrue(!file.exists())
        } finally {
            directory.deleteRecursively()
        }
    }

    private fun fixtureSnapshot(): CallHistoryCacheSnapshot {
        val page = CallRecordsPage.decode(json, ContentTestFixtures.text("call-records-page.json"))
        val detail = CallRecordDetail.decode(json, ContentTestFixtures.text("call-record-detail-ready.json"))
        val timeline = CallTimelinePage.decode(json, ContentTestFixtures.text("call-timeline-page.json"))
        return CallHistoryCacheSnapshot(
            deviceId = DEVICE_ID,
            records = listOf(detail.record) + page.items,
            collectionRevision = page.collectionRevision,
            details = mapOf(
                detail.record.callId to CachedCallDetail(
                    detail,
                    timeline.items,
                    timeline.nextCursor,
                    timeline.collectionRevision,
                ),
            ),
            savedAt = 1,
        )
    }

    private companion object {
        val json = Json { ignoreUnknownKeys = true }
        const val DEVICE_ID = "device_abcdefghijkl"
    }
}

private class CallHistoryTestCipher : ContentCipher {
    private val key = SecretKeySpec(ByteArray(32) { (it + 11).toByte() }, "AES")

    override fun encrypt(plaintext: ByteArray, aad: ByteArray): ByteArray = crypt(
        Cipher.ENCRYPT_MODE,
        plaintext,
        aad,
        ByteArray(12) { (it + 3).toByte() },
    )

    override fun decrypt(ciphertext: ByteArray, aad: ByteArray): ByteArray {
        require(ciphertext.size > 12)
        return crypt(
            Cipher.DECRYPT_MODE,
            ciphertext.copyOfRange(12, ciphertext.size),
            aad,
            ciphertext.copyOfRange(0, 12),
        )
    }

    private fun crypt(mode: Int, input: ByteArray, aad: ByteArray, iv: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(mode, key, GCMParameterSpec(128, iv))
        cipher.updateAAD(aad)
        val output = cipher.doFinal(input)
        return if (mode == Cipher.ENCRYPT_MODE) iv + output else output
    }
}
