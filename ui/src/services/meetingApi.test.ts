import { describe, it, expect, vi, beforeEach } from "vitest";
import { meetingApi } from "./meetingApi";
import { ApiError } from "../contracts/meetingContract";
import { mockMeetingSummaryCompleted } from "../test/fixtures/meetingFixtures";

describe("meetingApi", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("fetches meeting list with pagination cursor", async () => {
    const fakeResponse = {
      items: [mockMeetingSummaryCompleted],
      next_cursor: "cursor_123",
    };
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => fakeResponse,
    });

    const result = await meetingApi.fetchMeetings("cursor_abc", 10);
    expect(globalThis.fetch).toHaveBeenCalledWith("/api/v1/meetings?cursor=cursor_abc&limit=10");
    expect(result.items).toHaveLength(1);
    expect(result.next_cursor).toBe("cursor_123");
  });

  it("handles standard error envelope and throws ApiError", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({
        error: {
          code: "mode_conflict",
          message: "当前运行模式冲突",
          request_id: "req_test",
        },
      }),
    });

    await expect(meetingApi.fetchMeeting("m-1")).rejects.toThrow(ApiError);
    await expect(meetingApi.fetchMeeting("m-1")).rejects.toMatchObject({
      code: "mode_conflict",
      message: "当前运行模式冲突",
      requestId: "req_test",
    });
  });

  it("sends PATCH to update speaker display name", async () => {
    const updatedSpeaker = {
      speaker_key: "spk_0",
      default_label: "说话人 1",
      display_name: "王五",
      updated_at: "2026-08-21T10:00:00Z",
    };
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => updatedSpeaker,
    });

    const result = await meetingApi.updateSpeakerName("m-1", "spk_0", "王五");
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/v1/meetings/m-1/speakers/spk_0",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ display_name: "王五" }),
      }),
    );
    expect(result.display_name).toBe("王五");
  });

  it("sends POST to generate minutes with idempotency key", async () => {
    const fakeMinutes = {
      id: "min-1",
      meeting_id: "m-1",
      version: 2,
      status: "queued" as const,
      source_content_revision: 5,
      model: "qwen/qwen3.6-35b-a3b",
      content_json: null,
      content_markdown: null,
      created_at: "2026-08-21T10:00:00Z",
    };
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => fakeMinutes,
    });

    const result = await meetingApi.generateMinutes("m-1", "idem_key_1");
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/v1/meetings/m-1/minutes",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Idempotency-Key": "idem_key_1",
        }),
      }),
    );
    expect(result.version).toBe(2);
  });

  it("supports MeetingMockDataSource replay and operations", async () => {
    const { MeetingMockDataSource } = await import("./meetingMockDataSource");
    const mockSource = new MeetingMockDataSource({ delayMs: 1 });

    const list = await mockSource.fetchMeetings();
    expect(list.items.length).toBeGreaterThan(0);

    const meeting = await mockSource.fetchMeeting("mock-id");
    expect(meeting.id).toBe("mock-id");

    const transcript = await mockSource.fetchTranscript("mock-id");
    expect(transcript.segments.length).toBeGreaterThan(0);

    const events: any[] = [];
    const unsubscribe = mockSource.subscribeMeetingEvents("mock-id", (evt) => {
      events.push(evt);
    });

    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(events.length).toBeGreaterThan(0);
    unsubscribe();
    mockSource.stopAll();
  });
});
