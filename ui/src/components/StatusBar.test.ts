import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandSocketApi } from "../hooks/useCommandSocket";
import type { RuntimeStateSnapshot } from "../protocol";
import StatusBar, { sessionElapsedSeconds } from "./StatusBar";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const assistantSnapshot: RuntimeStateSnapshot = {
  mode: "assistant",
  pcm_owner: "assistant",
  pipeline: "listening",
  subtitle: "idle",
  mic_muted: false,
  runtime_revision: 1,
};

const commandSocket: CommandSocketApi = {
  state: "open",
  ready: true,
  snapshot: assistantSnapshot,
  highestRuntimeRevision: 1,
  sendCommand: vi.fn().mockResolvedValue(assistantSnapshot),
  reconcileRuntime: vi.fn().mockResolvedValue(assistantSnapshot),
};

describe("sessionElapsedSeconds", () => {
  it("derives elapsed time from the authoritative server timestamp", () => {
    expect(sessionElapsedSeconds("2026-08-21T00:00:00.000Z", Date.parse("2026-08-21T00:01:05.900Z")))
      .toBe(65);
  });

  it("resets when the session is stopped or the timestamp is invalid", () => {
    expect(sessionElapsedSeconds(null)).toBe(0);
    expect(sessionElapsedSeconds("invalid")).toBe(0);
  });
});

describe("StatusBar workspace switching state", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ services: [] }),
    }));
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.unstubAllGlobals();
  });

  function renderStatusBar(
    pendingTab: "assistant" | "subtitles" | null,
    reconciling: boolean,
    switchError: string | null = null,
  ): void {
    act(() => {
      root.render(createElement(StatusBar, {
        commandSocket,
        activeTab: "assistant",
        pendingTab,
        reconciling,
        switchError,
        onTabChange: vi.fn(),
      }));
    });
  }

  it("disables mode buttons and shows switching progress while a mode is pending", () => {
    renderStatusBar("subtitles", false);

    const buttons = Array.from(container.querySelectorAll("button"));
    const assistantButton = buttons.find((button) => button.textContent?.includes("语音助手"));
    const meetingButton = buttons.find((button) => button.textContent?.includes("会议助手"));
    const subtitlesButton = buttons.find((button) => button.textContent?.includes("实时字幕"));

    expect(assistantButton?.disabled).toBe(true);
    expect(subtitlesButton?.disabled).toBe(true);
    expect(meetingButton?.disabled).toBe(false);
    expect(container.textContent).toContain("切换中");
  });

  it("shows reconcile and explicit failure states", () => {
    renderStatusBar("subtitles", true);
    expect(container.textContent).toContain("正在对账");

    renderStatusBar(null, false, "模式已被占用");
    expect(container.querySelector("[role='alert']")?.textContent).toContain("模式已被占用");
  });
});
