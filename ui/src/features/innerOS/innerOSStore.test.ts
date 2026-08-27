import { describe, it, expect, beforeEach, vi } from "vitest";
import { useInnerOSStore } from "./innerOSStore";
import { innerOSApi } from "./api";

vi.mock("./api", () => ({
  innerOSApi: {
    saveExchange: vi.fn(),
    deleteExchange: vi.fn(),
    listExchanges: vi.fn(),
    getExchange: vi.fn(),
  },
}));

describe("useInnerOSStore", () => {
  beforeEach(() => {
    useInnerOSStore.getState().reset();
    vi.clearAllMocks();
  });

  it("handles complete query lifecycle: start -> generating -> completed", () => {
    const store = useInnerOSStore.getState();
    store.startQuery("q-1", "m-1", "刚才谈了什么？", "mixed");

    expect(useInnerOSStore.getState().queryStatus).toBe("accepted");
    expect(useInnerOSStore.getState().activeQueryId).toBe("q-1");

    useInnerOSStore.getState().setGenerating("q-1");
    expect(useInnerOSStore.getState().queryStatus).toBe("generating");

    const answer = {
      intent: "mixed" as const,
      evidence: [],
      facts: [{ text: "事实A", evidence_segment_ids: ["s1"] }],
      judgements: [],
      draft: null,
      limitations: [],
    };
    useInnerOSStore.getState().setCompleted("q-1", answer);

    expect(useInnerOSStore.getState().queryStatus).toBe("completed");
    expect(useInnerOSStore.getState().activeAnswer).toEqual(answer);
    expect(useInnerOSStore.getState().unsavedExchanges.length).toBe(1);
    expect(useInnerOSStore.getState().unsavedExchanges[0].queryId).toBe("q-1");
  });

  it("handles failure and cancellation", () => {
    const store = useInnerOSStore.getState();
    store.startQuery("q-2", "m-1", "测试失败", "fact");
    store.setFailed("q-2", "inner_os_insufficient_evidence", "证据不足");

    expect(useInnerOSStore.getState().queryStatus).toBe("failed");
    expect(useInnerOSStore.getState().activeError?.code).toBe("inner_os_insufficient_evidence");

    store.startQuery("q-3", "m-1", "测试取消", "fact");
    store.setCancelled("q-3");
    expect(useInnerOSStore.getState().queryStatus).toBe("cancelled");
  });

  it("saves exchange via API and updates unsavedExchanges and historyList", async () => {
    const mockSaved = {
      id: "q-4",
      meeting_id: "m-1",
      question: "保存问题",
      intent: "fact" as const,
      answer: {
        intent: "fact" as const,
        evidence: [],
        facts: [],
        judgements: [],
        draft: null,
        limitations: [],
      },
      source_transcript_revision: 1,
      source_content_revision: 1,
      used_ephemeral_context: false,
      model: "qwen",
      reasoning: "off" as const,
      created_at: "2026-08-27T00:00:00Z",
    };

    (innerOSApi.saveExchange as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce(mockSaved);

    const store = useInnerOSStore.getState();
    store.startQuery("q-4", "m-1", "保存问题", "fact");
    store.setCompleted("q-4", mockSaved.answer);

    await store.saveExchangeAction("m-1", "q-4");

    expect(useInnerOSStore.getState().activeAnswerSaved).toBe(true);
    expect(useInnerOSStore.getState().unsavedExchanges[0].saved).toBe(true);
    expect(useInnerOSStore.getState().historyList.length).toBe(1);
  });
});
