package ai.bondings.callpilot.diagnostics

import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class DiagLogTest {
    @get:Rule val temporary = TemporaryFolder()

    @Test fun rotatesAndBoundsEvenOversizedUtf8Entries() {
        val dir = temporary.newFolder()
        val store = DiagStore(dir, segmentBytes = 128, segments = 4)
        repeat(50) { store.append("entry=$it " + "x".repeat(40)) }
        assertTrue(dir.listFiles()!!.sumOf { it.length() } <= 512)
        assertFalse(store.snapshot().contains("entry=0 "))
        assertTrue(store.snapshot().contains("entry=49 "))
        store.append("中".repeat(1000))
        assertTrue(dir.listFiles()!!.sumOf { it.length() } <= 512)
        assertFalse(store.snapshot().contains('\uFFFD'))
    }

    @Test fun redactsPhoneAndCredentials() {
        val safe = DiagLog.redact("number=+8655500005678 token=secret access_token=abc Authorization: Bearer xyz password=pwd credential=cred")
        assertTrue(safe.contains("***5678"))
        listOf("8655500005678", "secret", "abc", "xyz", "pwd").forEach {
            assertFalse("leaked $it", safe.contains(it))
        }
        assertFalse(safe.contains("credential=cred"))
        assertFalse(DiagLog.redact("{\"token\":\"json-secret\"}").contains("json-secret"))
        assertEquals("***5678", DiagLog.phone("+86 555-0000-5678"))
    }

    @Test fun correlationIdsAreNotMistakenForPhoneNumbers() {
        assertEquals("call_ab_123456789012", DiagLog.identifier("call_ab_123456789012"))
        assertEquals("[invalid_id]", DiagLog.identifier("id\nmalicious"))
        assertEquals("***5678", DiagLog.identifier("55500005678"))
    }

    @Test fun ringSurvivesReopeningAndExportsOldestFirst() {
        val dir = temporary.newFolder()
        val store = DiagStore(dir, segmentBytes = 12, segments = 2)
        store.append("first")
        store.append("second")
        val reopened = DiagStore(dir, segmentBytes = 12, segments = 2)
        assertEquals("first\nsecond\n", reopened.snapshot())
        reopened.append("third")
        assertFalse(reopened.snapshot().contains("first"))
        assertTrue(reopened.snapshot().indexOf("second") < reopened.snapshot().indexOf("third"))
    }

    @Test fun crashPersistsWithoutMessagesAndDelegatesOriginalThrowable() {
        val store = DiagStore(temporary.newFolder())
        val failure = IllegalStateException("token=secret private conversation", RuntimeException("password=pwd"))
        var delegated = false
        val thread = Thread.currentThread()
        val handler = DiagnosticCrashHandler(store, Thread.UncaughtExceptionHandler { t, e ->
            assertSame(thread, t)
            assertSame(failure, e)
            assertTrue(store.snapshot().contains("IllegalStateException"))
            delegated = true
        })
        handler.uncaughtException(thread, failure)
        assertTrue(delegated)
        assertFalse(store.snapshot().contains("secret"))
        assertFalse(store.snapshot().contains("private conversation"))
        assertTrue(store.consumeCrashMarker())
        assertFalse(store.consumeCrashMarker())
    }

    @Test fun crashDelegatesEvenWhenStorageFails() {
        val file = temporary.newFile()
        var delegated = false
        DiagnosticCrashHandler(DiagStore(file), Thread.UncaughtExceptionHandler { _, _ -> delegated = true })
            .uncaughtException(Thread.currentThread(), Error("failure"))
        assertTrue(delegated)
    }
}
