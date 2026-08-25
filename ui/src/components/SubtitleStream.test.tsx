import { describe, it, expect, vi, beforeEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import SubtitleStream from "./SubtitleStream";
import { useSubtitleStore } from "../stores/subtitleStore";

vi.mock("../hooks/useEventSocket", () => ({
  useEventSocket: () => ({ state: "open" }),
}));

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

describe("SubtitleStream workspace layout", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    useSubtitleStore.setState({
      lines: [],
      rawLines: [],
      partial: "",
      connected: false,
      starredIndices: new Set<number>(),
      clearedOffset: 0,
    });
    if (!window.requestAnimationFrame) {
      window.requestAnimationFrame = (callback: FrameRequestCallback) =>
        window.setTimeout(() => callback(Date.now()), 0);
    }
    return () => {
      act(() => {
        root.unmount();
      });
      container.remove();
    };
  });

  it("renders unified status, display, and export groups in the left sidebar", () => {
    act(() => {
      root.render(<SubtitleStream />);
    });

    expect(container.querySelector(".subtitle-workspace")).not.toBeNull();
    expect(container.querySelector(".subtitle-sidebar")).not.toBeNull();
    expect(container.querySelector(".subtitle-sidebar-group-status")).not.toBeNull();
    expect(container.querySelector(".subtitle-sidebar-group-display")).not.toBeNull();
    expect(container.querySelector(".subtitle-sidebar-group-actions")).not.toBeNull();
    expect(container.textContent).toContain("字幕状态");
    expect(container.textContent).toContain("显示与筛选");
    expect(container.textContent).toContain("输出与操作");
  });

  it("keeps subtitle filtering and font controls inside the settings group", () => {
    act(() => {
      root.render(<SubtitleStream />);
    });

    const settingsGroup = container.querySelector(".subtitle-sidebar-group-display");
    expect(settingsGroup?.querySelector(".subtitle-search-input")).not.toBeNull();
    expect(settingsGroup?.querySelector(".subtitle-speaker-select")).not.toBeNull();
    expect(settingsGroup?.querySelector(".subtitle-font-size-control")).not.toBeNull();
  });
});
