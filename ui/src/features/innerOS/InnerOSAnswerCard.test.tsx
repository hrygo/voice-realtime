import { describe, it, expect, vi, beforeEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { InnerOSAnswerCard } from "./InnerOSAnswerCard";
import type { InnerOSAnswer } from "./contracts";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

describe("InnerOSAnswerCard", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    return () => {
      act(() => {
        root.unmount();
      });
      container.remove();
    };
  });

  const mockAnswer: InnerOSAnswer = {
    intent: "mixed",
    evidence: [
      {
        segment_id: "s1",
        start_ms: 1000,
        end_ms: 4000,
        speaker_key: "k1",
        speaker_name: "李工",
        text: "性能达标",
        content_hash: "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      },
    ],
    facts: [{ text: "性能符合预期", evidence_segment_ids: ["s1"] }],
    judgements: [
      {
        text: "工期充足",
        basis_segment_ids: ["s1"],
        uncertainty: "low",
        uncertainty_reason: "已完成首轮压测",
      },
    ],
    draft: {
      text: "已确认性能符合预期。",
    },
    limitations: [],
  };

  it("renders facts, judgements, and draft correctly", () => {
    const onSave = vi.fn();
    act(() => {
      root.render(
        <InnerOSAnswerCard
          queryId="q-1"
          question="性能如何？"
          intent="mixed"
          answer={mockAnswer}
          saved={false}
          onSave={onSave}
        />,
      );
    });

    expect(container.textContent).toContain("性能如何？");
    expect(container.textContent).toContain("性能符合预期");
    expect(container.textContent).toContain("工期充足");
    expect(container.textContent).toContain("低不确定性");
    expect(container.textContent).toContain("已确认性能符合预期。");
  });

  it("triggers onSave when save button is clicked", () => {
    const onSave = vi.fn();
    act(() => {
      root.render(
        <InnerOSAnswerCard
          queryId="q-1"
          question="性能如何？"
          intent="mixed"
          answer={mockAnswer}
          saved={false}
          onSave={onSave}
        />,
      );
    });

    const saveBtn = container.querySelector(".inner-os-save-btn") as HTMLButtonElement;
    expect(saveBtn).not.toBeNull();
    act(() => {
      saveBtn.click();
    });
    expect(onSave).toHaveBeenCalledTimes(1);
  });
});
