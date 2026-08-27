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

// Mock socket hook
vi.mock("./useInnerOSSocket", () => ({
  useInnerOSSocket: () => ({
    isConnected: true,
    isLoopbackSecure: true,
    sendQuery: vi.fn(),
    sendCancel: vi.fn(),
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
      transcriptRevision: 1,
      starredMap: {
        [mId]: new Set(["s1", "s2"]),
      },
    });
    useInnerOSStore.getState().reset();
    useInnerOSStore.setState({ isPanelOpen: true });

    return () => {
      act(() => {
        root.unmount();
      });
      container.remove();
    };
  });

  it("renders header, ephemeral drawer, quick section, and dock", () => {
    act(() => {
      root.render(<InnerOSPanel />);
    });

    expect(container.textContent).toContain("内心 OS · 私密副驾驶");
    expect(container.textContent).toContain("本次临时目标/背景");
    expect(container.textContent).toContain("回顾刚才结论");
    expect(container.textContent).toContain("已标记 2 段重点发言");

    const textarea = container.querySelector(".inner-os-textarea") as HTMLTextAreaElement;
    expect(textarea).not.toBeNull();
  });

  it("toggles ephemeral context drawer", () => {
    act(() => {
      root.render(<InnerOSPanel />);
    });

    const toggleBtn = container.querySelector(".inner-os-ephemeral-toggle") as HTMLButtonElement;
    expect(toggleBtn).not.toBeNull();

    act(() => {
      toggleBtn.click();
    });

    expect(container.textContent).toContain("核心目标 (Goal)");
    expect(container.textContent).toContain("关键议题 (Agenda)");
    expect(container.textContent).toContain("私密背景/底线 (Background)");
  });

  it("renders unified edge toggle handle and closes on click", () => {
    act(() => {
      root.render(<InnerOSPanel />);
    });

    const edgeHandle = container.querySelector(".inner-os-edge-toggle-handle") as HTMLElement;
    expect(edgeHandle).not.toBeNull();

    act(() => {
      edgeHandle.click();
    });

    expect(useInnerOSStore.getState().isPanelOpen).toBe(false);
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
});
