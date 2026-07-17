package ai.bondings.callpilot.content

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.io.File
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

interface ContentCipher {
    fun encrypt(plaintext: ByteArray, aad: ByteArray): ByteArray
    fun decrypt(ciphertext: ByteArray, aad: ByteArray): ByteArray
}

internal fun clearAllBestEffort(
    clearMessages: () -> Unit,
    clearCallHistory: () -> Unit,
) {
    runCatching(clearMessages)
    runCatching(clearCallHistory)
}

/** Encrypted, device-bound, no-backup content file with atomic replacement. */
class ProtectedJsonStore(
    private val file: File,
    private val cipher: ContentCipher,
) {
    fun read(deviceId: String): ByteArray? {
        if (!file.isFile) return null
        return try {
            cipher.decrypt(file.readBytes(), deviceId.encodeToByteArray())
        } catch (_: Exception) {
            clear()
            null
        }
    }

    fun write(deviceId: String, plaintext: ByteArray) {
        file.parentFile?.mkdirs()
        val temporary = File(file.parentFile, ".${file.name}.${System.nanoTime()}.tmp")
        try {
            temporary.outputStream().use { output ->
                output.write(cipher.encrypt(plaintext, deviceId.encodeToByteArray()))
                output.flush()
                (output as? java.io.FileOutputStream)?.fd?.sync()
            }
            try {
                Files.move(
                    temporary.toPath(),
                    file.toPath(),
                    StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING,
                )
            } catch (_: AtomicMoveNotSupportedException) {
                Files.move(temporary.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING)
            }
        } finally {
            temporary.delete()
        }
    }

    fun clear() {
        if (file.exists() && !file.delete()) {
            // Fail closed if deletion is transiently denied: no readable content may remain.
            file.writeBytes(ByteArray(0))
        }
    }

    companion object {
        fun messages(context: Context): ProtectedJsonStore {
            val directory = File(context.noBackupFilesDir, "CallPilot")
            return ProtectedJsonStore(
                File(directory, "messages-v1.bin"),
                AndroidKeystoreContentCipher("callpilot-content-v1"),
            )
        }

        fun callHistory(context: Context): ProtectedJsonStore {
            val directory = File(context.noBackupFilesDir, "CallPilot")
            return ProtectedJsonStore(
                File(directory, "call-history-v1.bin"),
                AndroidKeystoreContentCipher("callpilot-call-history-v1"),
            )
        }

        fun clearAll(context: Context) {
            clearAllBestEffort(
                clearMessages = { messages(context).clear() },
                clearCallHistory = { callHistory(context).clear() },
            )
        }
    }
}

private class AndroidKeystoreContentCipher(
    private val alias: String,
) : ContentCipher {
    override fun encrypt(plaintext: ByteArray, aad: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        cipher.updateAAD(aad)
        return cipher.iv + cipher.doFinal(plaintext)
    }

    override fun decrypt(ciphertext: ByteArray, aad: ByteArray): ByteArray {
        require(ciphertext.size > IV_BYTES)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(
            Cipher.DECRYPT_MODE,
            key(),
            GCMParameterSpec(128, ciphertext.copyOfRange(0, IV_BYTES)),
        )
        cipher.updateAAD(aad)
        return cipher.doFinal(ciphertext.copyOfRange(IV_BYTES, ciphertext.size))
    }

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(
                alias,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return generator.generateKey()
    }

    private companion object {
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_BYTES = 12
    }
}
