# Content sync v1 fixtures

These JSON files are the shared synthetic contract examples for Edge, Cloud,
iOS and Android tests. They intentionally contain no real phone number, message,
transcript, credential or token.

| File | Contract case |
| --- | --- |
| `messages-page.json` | Flat inbox/outbox page; two carrier-like fragments remain separate |
| `call-records-page.json` | Agent call plus remote-handset call without AI content |
| `call-record-detail-pending.json` | Completed call before its asynchronous summary arrives |
| `call-record-detail-ready.json` | Same call and ID after the late summary changes revisions |
| `call-record-detail-no-transcript.json` | Normal remote-handset detail with no summary/transcript |
| `call-timeline-page.json` | Public transcript, triage, takeover and result union |
| `call-timeline-empty.json` | Valid empty timeline for a non-AI call |
| `edge-data-request.json` | Cloud-generated bounded `data.request` |
| `edge-data-response.json` | Matching successful `data.response` |
| `errors.json` | Stable public HTTP error examples |

Fixtures are normative for field names, required/null behavior and enum spelling.
They are not production seed data. See `docs/content-sync-protocol.md` for
authorization, cursor, byte and lifecycle semantics that JSON examples cannot
express.
