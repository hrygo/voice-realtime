import { describe, it, expect, vi, beforeEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { InnerOSHistoryTab } from "./InnerOSHistoryTab";
import { useInnerOSStore } from "./innerOSStore";
import type { InnerOSExchange } from "./contracts";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

describe("InnerOSHistoryTab", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    const exchange: InnerOSExchange = {
      id: "exchange-1",
      meeting_id: "meeting-1",
      question: "刚才确认了什么？",
      intent: "fact",
      answer: {
        intent: "fact",
        evidence: [],
        facts: [{ text: "已确认一个事实", evidence_segment_ids: [] }],
        judgements: [],
        draft: null,
        limitations: [],
      },
      source_transcript_revision: 1,
      source_content_revision: 1,
      used_ephemeral_context: false,
      model: "local",
      reasoning: "off",
      created_at: "2026-08-27T12:00:00.000Z",
    };

    useInnerOSStore.setState({
      historyList: [
        exchange,
        {
          ...exchange,
          id: "exchange-2",
          question: "原证据还有效吗？",
          evidence_invalidated: true,
        },
      ],
      isLoadingHistory: false,
      fetchHistory: vi.fn().mockResolvedValue(undefined),
      deleteExchangeAction: vi.fn().mockResolvedValue(undefined),
    });
  });

  it("keeps history compact until the user opens an answer", () => {
    act(() => {
      root.render(<InnerOSHistoryTab meetingId="meeting-1" />);
    });

    expect(container.querySelector(".inner-os-answer-card")).toBeNull();

    const summaryButton = container.querySelector(
      ".inner-os-history-item-toggle",
    ) as HTMLButtonElement;
    expect(summaryButton).not.toBeNull();

    act(() => {
      summaryButton.click();
    });

    expect(container.querySelector(".inner-os-answer-content")).not.toBeNull();
    expect(container.querySelector(".inner-os-answer-card")).toBeNull();

    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("uses compact status labels while keeping the full meaning available", () => {
    act(() => {
      root.render(<InnerOSHistoryTab meetingId="meeting-1" />);
    });

    expect(container.textContent).toContain("证据有效");
    expect(container.textContent).toContain("证据变更");
    expect(container.textContent).not.toContain("原证据段已变更/修正");
  });
});
