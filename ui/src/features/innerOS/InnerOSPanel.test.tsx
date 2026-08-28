import { describe, it, expect, beforeEach, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { InnerOSPanel } from "./InnerOSPanel";
import { useMeetingStore } from "../../stores/meetingStore";
import { useInnerOSStore } from "./innerOSStore";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const { sendQueryMock, sendCancelMock } = vi.hoisted(() => ({
  sendQueryMock: vi.fn(),
  sendCancelMock: vi.fn(),
}));

// Mock socket hook
vi.mock("./useInnerOSSocket", () => ({
  useInnerOSSocket: () => ({
    isConnected: true,
    isLoopbackSecure: true,
    sendQuery: sendQueryMock,
    sendCancel: sendCancelMock,
  }),
}));

describe("InnerOSPanel", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    const mId = "00000000-0000-0000-0000-000000000001";
    useMeetingStore.setState({
      activeMeetingId: mId,
      status: "recording",
      transcriptRevision: 7,
      starredMap: {
        [mId]: new Set(["s1", "s2"]),
      },
    });
    useInnerOSStore.getState().reset();
    useInnerOSStore.setState({ isPanelOpen: true });
    sendQueryMock.mockClear();
    sendCancelMock.mockClear();

    return () => {
      act(() => {
        root.unmount();
      });
      container.remove();
      document.querySelectorAll(".inner-os-panel").forEach((el) => el.remove());
    };
  });

  it("renders header, ephemeral drawer, quick section, and dock", () => {
    act(() => {
      root.render(<InnerOSPanel />);
    });

    expect(document.body.textContent).toContain("内心 OS · 私密副驾驶");
    expect(document.body.textContent).toContain("会前底牌与目标");
    expect(document.body.textContent).toContain("回顾刚才结论");
    expect(document.body.textContent).toContain("重点 2");
    expect(document.body.textContent).not.toContain("已标记 2 段重点发言");
    expect(document.body.textContent).not.toContain("(优先检索中)");
    expect(document.body.textContent).not.toContain("v1");

    const textarea = document.body.querySelector(".inner-os-textarea") as HTMLTextAreaElement;
    expect(textarea).not.toBeNull();
    expect(document.body.querySelector(".inner-os-shortcut-hint")).toBeNull();

    act(() => {
      textarea.focus();
    });

    expect(document.body.querySelector(".inner-os-shortcut-hint")).not.toBeNull();

    expect(document.body.querySelector(".inner-os-submit-btn svg")).not.toBeNull();
    expect(document.body.querySelector(".inner-os-submit-btn")?.textContent).not.toContain("→");
  });

  it("toggles ephemeral context drawer", () => {
    act(() => {
      root.render(<InnerOSPanel />);
    });

    const toggleBtn = document.body.querySelector(".inner-os-ephemeral-toggle") as HTMLButtonElement;
    expect(toggleBtn).not.toBeNull();

    act(() => {
      toggleBtn.click();
    });

    expect(document.body.textContent).toContain("核心目标");
    expect(document.body.textContent).toContain("关键议题");
    expect(document.body.textContent).toContain("私密底线");
  });

  it("sends the ephemeral context version instead of the transcript revision", () => {
    act(() => {
      root.render(<InnerOSPanel />);
    });

    const quickQuery = Array.from(document.body.querySelectorAll(".inner-os-quick-pill-btn")).find(
      (button) => button.textContent?.includes("回顾刚才结论"),
    ) as HTMLButtonElement;
    expect(quickQuery).not.toBeUndefined();

    act(() => {
      quickQuery.click();
    });

    expect(sendQueryMock).toHaveBeenCalledWith(
      expect.any(String),
      "fact",
      1,
      null,
      ["s1", "s2"],
    );
  });

  it("renders close button and closes on click", () => {
    act(() => {
      root.render(<InnerOSPanel />);
    });

    const closeBtn = document.body.querySelector(".inner-os-close-btn") as HTMLElement;
    expect(closeBtn).not.toBeNull();
    expect(closeBtn.getAttribute("aria-label")).toBe("收起内心 OS 面板");

    act(() => {
      closeBtn.click();
    });

    expect(useInnerOSStore.getState().isPanelOpen).toBe(false);
  });

  it("provides an explicit accessible name for the question input", () => {
    act(() => {
      root.render(<InnerOSPanel />);
    });

    expect(document.body.querySelector(".inner-os-textarea")?.getAttribute("aria-label")).toBe(
      "向内心 OS 提问",
    );
  });

  it("exposes connection and generation states as live status", () => {
    act(() => {
      root.render(<InnerOSPanel />);
    });

    const subtitle = document.body.querySelector(".inner-os-subtitle");
    expect(subtitle?.getAttribute("role")).toBe("status");
    expect(subtitle?.getAttribute("aria-live")).toBe("polite");

    act(() => {
      useInnerOSStore.setState({
        activeQueryId: "q-1",
        activeQuestion: "问题",
        queryStatus: "generating",
      });
    });

    expect(document.body.querySelector(".inner-os-generating-box")?.getAttribute("role")).toBe(
      "status",
    );
  });

  it("shows a recoverable message after a query is cancelled", () => {
    useInnerOSStore.setState({ queryStatus: "cancelled" });

    act(() => {
      root.render(<InnerOSPanel />);
    });

    expect(document.body.textContent).toContain("本次研判已取消，可继续提问");
  });

  it("closes panel on Escape key", () => {
    act(() => {
      root.render(<InnerOSPanel />);
    });

    expect(useInnerOSStore.getState().isPanelOpen).toBe(true);

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });

    expect(useInnerOSStore.getState().isPanelOpen).toBe(false);
  });

  it("closes panel on pointerdown outside", () => {
    vi.useFakeTimers();
    act(() => {
      root.render(<InnerOSPanel />);
    });

    act(() => {
      vi.advanceTimersByTime(30);
    });

    expect(useInnerOSStore.getState().isPanelOpen).toBe(true);

    const outsideDiv = document.createElement("div");
    document.body.appendChild(outsideDiv);

    act(() => {
      outsideDiv.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    });

    expect(useInnerOSStore.getState().isPanelOpen).toBe(false);
    outsideDiv.remove();
    vi.useRealTimers();
  });

  it("renders multi-turn stream cards and export notes action", () => {
    const exchange1 = {
      queryId: "q-stream-1",
      meetingId: "meeting-1",
      question: "第一轮研判问题",
      intent: "fact" as const,
      answer: {
        intent: "fact" as const,
        evidence: [],
        facts: [{ text: "事实结论一", evidence_segment_ids: [] }],
        judgements: [],
        draft: null,
        limitations: [],
      },
      createdAt: "2026-08-27T00:00:00Z",
      saved: false,
    };
    const exchange2 = {
      queryId: "q-stream-2",
      meetingId: "meeting-1",
      question: "第二轮研判问题",
      intent: "draft" as const,
      answer: {
        intent: "draft" as const,
        evidence: [],
        facts: [],
        judgements: [],
        draft: { text: "草稿结论二" },
        limitations: [],
      },
      createdAt: "2026-08-27T00:01:00Z",
      saved: true,
    };

    useInnerOSStore.setState({
      unsavedExchanges: [exchange1, exchange2],
    });

    act(() => {
      root.render(<InnerOSPanel />);
    });

    expect(document.body.textContent).toContain("第一轮研判问题");
    expect(document.body.textContent).toContain("第二轮研判问题");
    expect(document.body.textContent).toContain("导出笔记");

    const exportBtn = Array.from(document.body.querySelectorAll(".inner-os-tool-btn")).find(
      (b) => b.textContent?.includes("导出笔记"),
    ) as HTMLButtonElement;
    expect(exportBtn).not.toBeNull();
  });

  it("renders open in standalone tab button in header tools", () => {
    useInnerOSStore.setState({ isPanelOpen: true });
    const windowOpenSpy = vi.spyOn(window, "open").mockImplementation(() => null);

    act(() => {
      root.render(<InnerOSPanel />);
    });

    const openTabBtn = Array.from(document.body.querySelectorAll(".inner-os-tool-btn")).find(
      (b) => b.textContent?.includes("独立窗口"),
    ) as HTMLButtonElement;
    expect(openTabBtn).not.toBeNull();

    act(() => {
      openTabBtn.click();
    });

    expect(windowOpenSpy).toHaveBeenCalledWith(
      expect.stringContaining("view=inner-os"),
      "_blank",
      expect.any(String),
    );
    windowOpenSpy.mockRestore();
  });

  it("renders in standalone mode without close button and edge handle", () => {
    useInnerOSStore.setState({ isPanelOpen: false });

    act(() => {
      root.render(<InnerOSPanel isStandalone />);
    });

    const panel = container.querySelector(".inner-os-panel");
    expect(panel).not.toBeNull();
    expect(panel?.classList.contains("is-standalone")).toBe(true);
    expect(container.querySelector(".inner-os-close-btn")).toBeNull();
    expect(container.querySelector(".inner-os-edge-toggle-handle")).toBeNull();
    expect(panel?.getAttribute("data-standalone")).toBe("true");
  });
});
