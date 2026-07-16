import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  CONTENT_WIRE_LIMIT_BYTES,
  callRecordDetailSchema,
  callRecordsPageSchema,
  callTimelinePageSchema,
  contentCursorSchema,
  dataRequestSchema,
  dataResponseSchema,
  isBeforeRelayDeadline,
  messagesPageSchema,
  responseMatchesRequest,
  serializedByteLength
} from "../src/content-sync";
import { edgeMessageSchema } from "../src/schemas";

const fixtureDir = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../docs/fixtures/content-sync/v1"
);

function fixture(name: string): unknown {
  return JSON.parse(readFileSync(join(fixtureDir, name), "utf8"));
}

describe("content sync v1 shared fixtures", () => {
  it("validates every success DTO fixture against the Cloud boundary", () => {
    expect(messagesPageSchema.safeParse(fixture("messages-page.json")).success).toBe(true);
    expect(callRecordsPageSchema.safeParse(fixture("call-records-page.json")).success).toBe(true);
    expect(callRecordDetailSchema.safeParse(fixture("call-record-detail-pending.json")).success).toBe(true);
    expect(callRecordDetailSchema.safeParse(fixture("call-record-detail-ready.json")).success).toBe(true);
    expect(callRecordDetailSchema.safeParse(fixture("call-record-detail-no-transcript.json")).success).toBe(true);
    expect(callTimelinePageSchema.safeParse(fixture("call-timeline-page.json")).success).toBe(true);
    expect(callTimelinePageSchema.safeParse(fixture("call-timeline-empty.json")).success).toBe(true);
  });

  it("validates the shared relay envelopes and preserves additive DTO fields", () => {
    expect(dataRequestSchema.safeParse(fixture("edge-data-request.json")).success).toBe(true);
    const response = fixture("edge-data-response.json") as Record<string, unknown>;
    const body = response.body as Record<string, unknown>;
    const items = body.items as Array<Record<string, unknown>>;
    items[0] = { ...items[0], futureDisplayHint: "safe additive value" };
    const parsed = dataResponseSchema.parse(response);
    expect(parsed.status).toBe("ok");
    if (parsed.status === "ok" && parsed.resource === "messages.list") {
      expect(parsed.body.items[0]?.futureDisplayHint).toBe("safe additive value");
    }
    expect(edgeMessageSchema.safeParse(response).success).toBe(true);
  });

  it("fails closed on malformed page and relay discriminators", () => {
    const page = fixture("messages-page.json") as Record<string, unknown>;
    expect(messagesPageSchema.safeParse({ ...page, hasMore: false }).success).toBe(false);
    expect(dataRequestSchema.safeParse({
      ...(fixture("edge-data-request.json") as Record<string, unknown>),
      resource: "filesystem.read"
    }).success).toBe(false);
    expect(dataRequestSchema.safeParse({
      ...(fixture("edge-data-request.json") as Record<string, unknown>),
      v: 2
    }).success).toBe(false);
    const response = fixture("edge-data-response.json") as Record<string, unknown>;
    expect(dataResponseSchema.safeParse({ ...response, error: { code: "NOT_FOUND" } }).success).toBe(false);
    expect(edgeMessageSchema.safeParse({ v: 1, type: "data.chunk", body: "secret" }).success).toBe(false);
  });

  it("passes future timeline types but never accepts an incomplete known type", () => {
    const page = fixture("call-timeline-empty.json") as Record<string, unknown>;
    expect(callTimelinePageSchema.safeParse({
      ...page,
      items: [{
        timelineItemId: "item_fixture_future_0001",
        occurredAt: 1784161010000,
        type: "FUTURE_EVENT",
        additiveValue: true
      }]
    }).success).toBe(true);
    expect(callTimelinePageSchema.safeParse({
      ...page,
      items: [{
        timelineItemId: "item_fixture_known_0001",
        occurredAt: 1784161010000,
        type: "TRANSCRIPT"
      }]
    }).success).toBe(false);
  });

  it("measures the exact UTF-8 wire bytes rather than UTF-16 characters", () => {
    expect(serializedByteLength("a".repeat(CONTENT_WIRE_LIMIT_BYTES))).toBe(CONTENT_WIRE_LIMIT_BYTES);
    expect(serializedByteLength("界".repeat(5_462))).toBe(16_386);
  });

  it("accepts bounded restart-stable cursors longer than entity ids", () => {
    const request = fixture("edge-data-request.json") as Record<string, unknown>;
    const longCursor = `cursor_${"a".repeat(512)}`;
    expect(dataRequestSchema.safeParse({
      ...request,
      params: { limit: 25, cursor: longCursor }
    }).success).toBe(true);
    expect(dataRequestSchema.safeParse({
      ...request,
      params: { limit: 25, cursor: `cursor_${"a".repeat(2_048)}` }
    }).success).toBe(false);
    expect(contentCursorSchema.safeParse("cursor_").success).toBe(false);
  });

  it("rejects a relay response at the exact deadline boundary", () => {
    expect(isBeforeRelayDeadline(1_001, 1_000)).toBe(true);
    expect(isBeforeRelayDeadline(1_000, 1_000)).toBe(false);
    expect(isBeforeRelayDeadline(999, 1_000)).toBe(false);
  });

  it("binds response item count and record identity to the exact request", () => {
    const request = dataRequestSchema.parse({
      ...(fixture("edge-data-request.json") as Record<string, unknown>),
      params: { limit: 1, cursor: null }
    });
    const response = dataResponseSchema.parse(fixture("edge-data-response.json"));
    expect(responseMatchesRequest(response, request)).toBe(true);
    if (response.status !== "ok" || response.resource !== "messages.list") {
      expect.fail("fixture must be a message response");
    }
    expect(responseMatchesRequest({
      ...response,
      body: { ...response.body, items: [response.body.items[0]!, response.body.items[0]!] }
    }, request)).toBe(false);

    const detail = dataResponseSchema.parse({
      v: 1,
      type: "data.response",
      requestId: "request_fixture_0001",
      resource: "call_records.get",
      status: "ok",
      body: fixture("call-record-detail-ready.json")
    });
    const detailRequest = dataRequestSchema.parse({
      v: 1,
      type: "data.request",
      requestId: "request_fixture_0001",
      deviceId: "device_fixture_0001",
      resource: "call_records.get",
      params: { callId: "call_fixture_other_0001" },
      issuedAtUnixMs: 1_000,
      expiresAtUnixMs: 2_000
    });
    expect(responseMatchesRequest(detail, detailRequest)).toBe(false);
  });

  it("requires detail summary presence to match its lifecycle", () => {
    const pending = fixture("call-record-detail-pending.json") as Record<string, unknown>;
    const ready = fixture("call-record-detail-ready.json") as Record<string, unknown>;
    expect(callRecordDetailSchema.safeParse({ ...pending, summary: (ready as { summary: unknown }).summary }).success).toBe(false);
    expect(callRecordDetailSchema.safeParse({ ...ready, summary: null }).success).toBe(false);
    expect(callRecordDetailSchema.safeParse(pending).success).toBe(true);
    expect(callRecordDetailSchema.safeParse(ready).success).toBe(true);
  });

  it("rejects RECEIVED status on an outbound message", () => {
    const page = fixture("messages-page.json") as { items: Array<Record<string, unknown>> };
    expect(messagesPageSchema.safeParse({
      ...page,
      items: [{ ...page.items[0], direction: "OUTBOUND", status: "RECEIVED" }]
    }).success).toBe(false);
  });

  it("preserves a bounded additive product status for client fallback", () => {
    const page = fixture("call-records-page.json") as { items: Array<Record<string, unknown>> };
    const parsed = callRecordsPageSchema.parse({
      ...page,
      items: [{ ...page.items[0], status: "FUTURE_NORMALIZED_STATUS" }]
    });
    expect(parsed.items[0]?.status).toBe("FUTURE_NORMALIZED_STATUS");
    expect(callRecordsPageSchema.safeParse({
      ...page,
      items: [{ ...page.items[0], status: "invalid status" }]
    }).success).toBe(false);
  });

  it("keeps shared fixtures synthetic and migrations free of content tables", () => {
    const serializedFixtures = readdirSync(fixtureDir)
      .filter((name) => name.endsWith(".json"))
      .map((name) => readFileSync(join(fixtureDir, name), "utf8"))
      .join("\n");
    // 用模式而非具体号码断言:fixtures 不得含任何大陆 11 位手机号或密钥字样。
    // (绝不把真实号码硬编码进源码——那本身就是泄漏。)
    expect(serializedFixtures).not.toMatch(
      /(?<![\d+])1[3-9]\d{9}(?!\d)|\+861[3-9]\d{9}(?!\d)|(?<!\d)10086(?!\d)|DASHSCOPE|OPENAI_API_KEY/
    );

    const migrations = join(dirname(fileURLToPath(import.meta.url)), "../migrations");
    const sql = readdirSync(migrations)
      .filter((name) => name.endsWith(".sql"))
      .map((name) => readFileSync(join(migrations, name), "utf8"))
      .join("\n");
    expect(sql).not.toMatch(/CREATE\s+TABLE\s+(?:messages|transcripts|timelines|summaries)\b/i);

    const wrangler = JSON.parse(readFileSync(join(
      dirname(fileURLToPath(import.meta.url)), "../wrangler.jsonc"
    ), "utf8")) as { observability?: { enabled?: boolean } };
    expect(wrangler.observability?.enabled).toBe(false);
  });
});
