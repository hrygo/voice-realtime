import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { innerOSApi } from "./api";
import { ApiError } from "../../contracts/meetingContract";

describe("Inner OS REST API Client", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("saveExchange sends PUT and returns exchange", async () => {
    const mockExchange = {
      id: "ex-1",
      meeting_id: "m-1",
      question: "结论是什么？",
      intent: "fact",
      answer: {
        intent: "fact",
        evidence: [],
        facts: [{ text: "事实1", evidence_segment_ids: ["seg-1"] }],
        judgements: [],
        draft: null,
        limitations: [],
      },
      source_transcript_revision: 1,
      source_content_revision: 1,
      used_ephemeral_context: false,
      model: "qwen",
      reasoning: "off",
      created_at: "2026-08-27T00:00:00Z",
    };

    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: () => Promise.resolve(mockExchange),
    });

    const res = await innerOSApi.saveExchange("m-1", "ex-1");
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/meetings/m-1/inner-os/exchanges/ex-1"),
      expect.objectContaining({ method: "PUT" }),
    );
    expect(res.id).toBe("ex-1");
  });

  it("deleteExchange returns void on 204", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 204,
    });

    await expect(innerOSApi.deleteExchange("m-1", "ex-1")).resolves.toBeUndefined();
  });

  it("throws ApiError with envelope details on failure", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: () =>
        Promise.resolve({
          error: {
            code: "inner_os_not_found",
            message: "未找到该问答记录",
            request_id: "req-err-1",
          },
        }),
    });

    await expect(innerOSApi.getExchange("m-1", "ex-999")).rejects.toThrow(ApiError);
  });
});
