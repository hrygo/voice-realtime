import { describe, it, expect, beforeEach, vi } from "vitest";
import { useInnerOSStore } from "./innerOSStore";
import { innerOSApi } from "./api";

vi.mock("./api", () => ({
  innerOSApi: {
    saveExchange: vi.fn(),
    deleteExchange: vi.fn(),
    listExchanges: vi.fn(),
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

  it("manages question navigation history and custom prompts", () => {
    const store = useInnerOSStore.getState();
    store.addQuestionHistory("问题 1");
    store.addQuestionHistory("问题 2");
    store.addQuestionHistory("问题 1"); // deduplicate to top

    expect(useInnerOSStore.getState().questionHistory).toEqual(["问题 1", "问题 2"]);

    store.addCustomPrompt("快速总结", "请总结当前结论", "fact");
    const prompts = useInnerOSStore.getState().customPrompts;
    expect(prompts.length).toBeGreaterThan(0);
    expect(prompts[0].label).toBe("快速总结");

    store.removeCustomPrompt(prompts[0].id);
    expect(useInnerOSStore.getState().customPrompts.length).toBe(prompts.length - 1);
  });

  it("exports session exchanges as structured Markdown notes", () => {
    const store = useInnerOSStore.getState();
    const answer = {
      intent: "mixed" as const,
      evidence: [],
      facts: [{ text: "延迟指标小于 15ms", evidence_segment_ids: ["s1"] }],
      judgements: [{ text: "排期较紧", basis_segment_ids: [], uncertainty: "medium" as const, uncertainty_reason: "工期未定" }],
      draft: { text: "我们建议周四先进行用例评审。" },
      limitations: [],
    };
    store.startQuery("q-md", "m-1", "网关延迟结论是什么？", "mixed");
    store.setCompleted("q-md", answer);

    const md = store.exportNotesAsMarkdown("网关架构评审会");
    expect(md).toContain("网关架构评审会 · 内心 OS 私密副驾驶笔记");
    expect(md).toContain("网关延迟结论是什么？");
    expect(md).toContain("延迟指标小于 15ms");
    expect(md).toContain("排期较紧");
    expect(md).toContain("我们建议周四先进行用例评审。");
  });

  it("saves all unsaved exchanges in batch", async () => {
    const mockSaved = (id: string) => ({
      id,
      meeting_id: "m-1",
      question: `问题 ${id}`,
      intent: "fact" as const,
      answer: { intent: "fact" as const, evidence: [], facts: [], judgements: [], draft: null, limitations: [] },
      source_transcript_revision: 1,
      source_content_revision: 1,
      used_ephemeral_context: false,
      model: "qwen",
      reasoning: "off" as const,
      created_at: "2026-08-27T00:00:00Z",
    });

    (innerOSApi.saveExchange as unknown as ReturnType<typeof vi.fn>)
      .mockImplementation((_, exchangeId) => Promise.resolve(mockSaved(exchangeId)));

    const store = useInnerOSStore.getState();
    store.startQuery("q-1", "m-1", "问答1", "fact");
    store.setCompleted("q-1", mockSaved("q-1").answer);
    store.startQuery("q-2", "m-1", "问答2", "fact");
    store.setCompleted("q-2", mockSaved("q-2").answer);

    const count = await store.saveAllExchangesAction("m-1");
    expect(count).toBe(2);
    expect(useInnerOSStore.getState().unsavedExchanges.every((i) => i.saved)).toBe(true);
  });
});
