import { describe, it, expect, vi, beforeEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { InnerOSAnswerContent } from "./InnerOSAnswerContent";
import type { InnerOSAnswer } from "./contracts";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

describe("InnerOSAnswerContent", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  const answer: InnerOSAnswer = {
    intent: "mixed",
    evidence: [
      {
        segment_id: "segment-1",
        start_ms: 1_000,
        end_ms: 2_000,
        speaker_key: "speaker-1",
        speaker_name: "发言人 1",
        text: "已确认性能指标。",
        content_hash: "sha256:content",
      },
    ],
    facts: [{ text: "性能指标已确认", evidence_segment_ids: ["segment-1"] }],
    judgements: [],
    draft: { text: "我会按已确认指标推进。" },
    limitations: [{ code: "context_truncated", message: "较早的会议内容未纳入本次研判。" }],
  };

  it("renders answer sections without card-level actions", () => {
    act(() => {
      root.render(<InnerOSAnswerContent answer={answer} compact />);
    });

    expect(container.textContent).toContain("事实依据");
    expect(container.textContent).toContain("性能指标已确认");
    expect(container.textContent).toContain("建议发言草稿");
    expect(container.textContent).toContain("较早的会议内容未纳入本次研判");
    expect(container.querySelector(".inner-os-save-btn")).toBeNull();
    expect(container.querySelector(".inner-os-followup-btn")).toBeNull();

    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("forwards evidence selection from compact content", () => {
    const onSelectEvidence = vi.fn();
    act(() => {
      root.render(
        <InnerOSAnswerContent
          answer={answer}
          compact
          onSelectEvidence={onSelectEvidence}
        />,
      );
    });

    const evidenceButton = container.querySelector(
      '[data-testid="evidence-pill-segment-1"]',
    ) as HTMLButtonElement;
    expect(evidenceButton).not.toBeNull();

    act(() => {
      evidenceButton.click();
    });

    expect(onSelectEvidence).toHaveBeenCalledWith("segment-1");

    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("supports tone variations and draft copy in non-compact mode", () => {
    act(() => {
      root.render(<InnerOSAnswerContent answer={answer} compact={false} />);
    });

    expect(container.textContent).toContain("语气:");
    expect(container.textContent).toContain("标准");
    expect(container.textContent).toContain("简短");

    const conciseChip = Array.from(container.querySelectorAll(".inner-os-tone-chip")).find(
      (el) => el.textContent?.includes("简短"),
    ) as HTMLButtonElement;
    expect(conciseChip).not.toBeNull();

    act(() => {
      conciseChip.click();
    });

    expect(conciseChip.classList.contains("is-active")).toBe(true);

    act(() => {
      root.unmount();
    });
    container.remove();
  });
});
