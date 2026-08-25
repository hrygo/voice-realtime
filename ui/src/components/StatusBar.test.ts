import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandSocketApi } from "../hooks/useCommandSocket";
import type { RuntimeStateSnapshot } from "../protocol";
import { useUISettingsStore } from "../stores/uiSettingsStore";
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

describe("StatusBar service diagnostics", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
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

  async function renderServices(services: readonly object[]): Promise<void> {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ services }),
    }));

    await act(async () => {
      root.render(createElement(StatusBar, { commandSocket }));
    });

    const healthButton = Array.from(container.querySelectorAll("button"))
      .find((button) => button.title.includes("点击查看全系统"));
    expect(healthButton).toBeDefined();
    act(() => {
      healthButton?.click();
    });
  }

  function findServiceRow(name: string): HTMLDivElement | undefined {
    return Array.from(container.querySelectorAll<HTMLDivElement>(".health-popover-row"))
      .find((row) => row.textContent?.includes(name));
  }

  it("shows WLK process and voice workload diagnostics without changing the process light", async () => {
    await renderServices([
      {
        name: "wlk",
        status: "ok",
        url: "http://127.0.0.1:8001",
        workload: "degraded",
        ws_state: "reconnecting",
        reconnect_count: 3,
        last_event_age_ms: 12500,
        dropped_chunks: 4,
        gap_count: 2,
      },
    ]);

    const row = findServiceRow("WhisperLiveKit");
    expect(row?.textContent).toContain("HTTP 进程状态：运行正常");
    expect(row?.textContent).toContain("语音工作负载：degraded");
    expect(row?.textContent).toContain("WebSocket 状态：reconnecting");
    expect(row?.textContent).toContain("重连次数：3");
    expect(row?.textContent).toContain("距最近事件：12500 ms");
    expect(row?.textContent).toContain("丢弃音频块：4");
    expect(row?.textContent).toContain("音频缺口：2");
    expect(row?.querySelector(".light-dot")?.classList.contains("dot-ok")).toBe(true);
  });

  it("shows a long last-event age as an unclassified raw value", async () => {
    await renderServices([
      {
        name: "wlk",
        status: "ok",
        url: "http://127.0.0.1:8001",
        workload: "degraded",
        last_event_age_ms: 987654,
      },
    ]);

    const row = findServiceRow("WhisperLiveKit");
    const ageDetail = Array.from(row?.querySelectorAll<HTMLElement>(".health-row-detail") ?? [])
      .find((detail) => detail.textContent?.includes("距最近事件：987654 ms"));
    expect(ageDetail).toBeDefined();
    expect(ageDetail?.className).not.toContain("error");
    expect(row?.textContent).not.toContain("关闭游戏");
    expect(row?.textContent).not.toContain("CPU");
    expect(row?.textContent).not.toContain("GPU");
  });

  it("keeps rendering the legacy three-service response", async () => {
    await renderServices([
      { name: "wlk", status: "ok", url: "http://127.0.0.1:8001" },
      { name: "tts", status: "timeout", url: "http://127.0.0.1:8765" },
      { name: "lm", status: "unreachable", url: "http://127.0.0.1:1234" },
    ]);

    expect(findServiceRow("WhisperLiveKit")?.textContent).toContain("HTTP 进程状态：运行正常");
    expect(findServiceRow("Qwen3-TTS 桥")?.textContent).toContain("连接超时");
    expect(findServiceRow("LM Studio")?.textContent).toContain("服务未启动");
    expect(container.querySelectorAll(".health-popover-row")).toHaveLength(7);
  });

  it("renders null and unknown workload states defensively", async () => {
    await renderServices([
      {
        name: "wlk",
        status: "ok",
        url: "http://127.0.0.1:8001",
        workload: null,
        ws_state: null,
      },
      {
        name: "wlk-future",
        status: "ok",
        url: "http://127.0.0.1:8002",
        workload: "future-workload",
        ws_state: "future-ws-state",
      },
    ]);

    const wlkRow = findServiceRow("WhisperLiveKit");
    expect(wlkRow?.textContent).toContain("语音工作负载：未知");
    expect(wlkRow?.textContent).toContain("WebSocket 状态：未知");

    const futureRow = findServiceRow("wlk-future");
    expect(futureRow?.textContent).toContain("语音工作负载：future-workload");
    expect(futureRow?.textContent).toContain("WebSocket 状态：future-ws-state");
  });
});

describe("StatusBar aggregate health", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        services: [
          { name: "wlk", status: "ok", url: "http://127.0.0.1:8001" },
          { name: "tts", status: "ok", url: "http://127.0.0.1:8765" },
          { name: "lm", status: "ok", url: "http://127.0.0.1:1234" },
        ],
      }),
    }));
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

  async function renderHealth(
    mode: RuntimeStateSnapshot["mode"],
    pipelineStatus: string,
    subtitleStatus: string,
    activeTab: "assistant" | "meeting" | "subtitles",
  ): Promise<HTMLButtonElement> {
    useUISettingsStore.setState({
      pipelineStatus,
      subtitleStatus,
      storageHealth: "ok",
    });
    const snapshot = {
      ...assistantSnapshot,
      mode,
      pcm_owner: mode === "idle" ? "none" : mode,
    } as RuntimeStateSnapshot;

    await act(async () => {
      root.render(createElement(StatusBar, {
        commandSocket: { ...commandSocket, snapshot },
        activeTab,
      }));
    });

    const healthButton = container.querySelector<HTMLButtonElement>(".health-master-pill");
    expect(healthButton).not.toBeNull();
    return healthButton as HTMLButtonElement;
  }

  it("excludes paused subtitles from assistant aggregate health using authoritative mode", async () => {
    const healthButton = await renderHealth("assistant", "running", "paused", "subtitles");

    expect(healthButton.classList.contains("all-ok")).toBe(true);
    expect(healthButton.textContent).toContain("引擎全就绪 (6/6)");
  });

  it.each(["subtitles", "meeting"] as const)(
    "excludes a stopped pipeline from %s aggregate health",
    async (mode) => {
      const healthButton = await renderHealth(mode, "stopped", "connected", "assistant");

      expect(healthButton.classList.contains("all-ok")).toBe(true);
      expect(healthButton.textContent).toContain("引擎全就绪 (6/6)");
    },
  );

  it.each([
    ["assistant", "running", "error"],
    ["subtitles", "error", "connected"],
  ] as const)("keeps unexpected %s workload errors in aggregate health", async (
    mode,
    pipelineStatus,
    subtitleStatus,
  ) => {
    const healthButton = await renderHealth(mode, pipelineStatus, subtitleStatus, "meeting");

    expect(healthButton.classList.contains("has-error")).toBe(true);
    expect(healthButton.textContent).toContain("异常 (6/7)");
  });
});
