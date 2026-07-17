package ai.bondings.callpilot.content

import java.io.File
import java.nio.file.Files
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ProtectedJsonStoreTest {
    @Test
    fun `ciphertext hides content and round trips only for the same device`() {
        val directory = Files.createTempDirectory("callpilot-content-test").toFile()
        try {
            val store = ProtectedJsonStore(File(directory, "messages-v1.bin"), TestCipher())
            val plaintext = "synthetic private message".encodeToByteArray()

            store.write(DEVICE_ID, plaintext)

            val disk = File(directory, "messages-v1.bin").readBytes()
            assertFalse(disk.toString(Charsets.UTF_8).contains("synthetic private message"))
            assertArrayEquals(plaintext, store.read(DEVICE_ID))
            assertNull(store.read("device_otherdevice12"))
            assertFalse(File(directory, "messages-v1.bin").exists())
        } finally {
            directory.deleteRecursively()
        }
    }

    @Test
    fun `corrupt cache fails closed and is removed`() {
        val directory = Files.createTempDirectory("callpilot-content-test").toFile()
        try {
            val file = File(directory, "messages-v1.bin").also {
                it.writeText("not encrypted content")
            }
            val store = ProtectedJsonStore(file, TestCipher())

            assertNull(store.read(DEVICE_ID))
            assertTrue(!file.exists())
        } finally {
            directory.deleteRecursively()
        }
    }

    @Test
    fun `authenticated but semantically invalid snapshot is removed`() {
        val directory = Files.createTempDirectory("callpilot-content-test").toFile()
        try {
            val file = File(directory, "messages-v1.bin")
            val protected = ProtectedJsonStore(file, TestCipher())
            protected.write(
                DEVICE_ID,
                """{"deviceId":"$DEVICE_ID","messages":[],"watermark":null,"collectionRevision":null,"savedAt":-1}"""
                    .encodeToByteArray(),
            )

            val cache = MessageCacheStore(protected)

            assertNull(cache.load(DEVICE_ID))
            assertFalse(file.exists())
        } finally {
            directory.deleteRecursively()
        }
    }

    private companion object {
        const val DEVICE_ID = "device_abcdefghijkl"
    }
}

private class TestCipher : ContentCipher {
    private val key = SecretKeySpec(ByteArray(32) { (it + 1).toByte() }, "AES")

    override fun encrypt(plaintext: ByteArray, aad: ByteArray): ByteArray = crypt(
        Cipher.ENCRYPT_MODE,
        plaintext,
        aad,
        ByteArray(12) { (it + 7).toByte() },
    )

    override fun decrypt(ciphertext: ByteArray, aad: ByteArray): ByteArray {
        require(ciphertext.size > 12)
        return crypt(Cipher.DECRYPT_MODE, ciphertext.copyOfRange(12, ciphertext.size), aad, ciphertext.copyOfRange(0, 12))
    }

    private fun crypt(mode: Int, input: ByteArray, aad: ByteArray, iv: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(mode, key, GCMParameterSpec(128, iv))
        cipher.updateAAD(aad)
        val output = cipher.doFinal(input)
        return if (mode == Cipher.ENCRYPT_MODE) iv + output else output
    }
}
