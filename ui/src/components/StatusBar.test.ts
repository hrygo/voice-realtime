import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandSocketApi } from "../hooks/useCommandSocket";
import type { RuntimeStateSnapshot } from "../protocol";
import { useUISettingsStore } from "../stores/uiSettingsStore";
import { useMeetingStore } from "../stores/meetingStore";
import StatusBar, { getStatusModePresentation, sessionElapsedSeconds } from "./StatusBar";


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

describe("getStatusModePresentation", () => {
  it("keeps voice assistant phase labels explicit", () => {
    expect(getStatusModePresentation({
      activeTab: "assistant",
      meetingStatus: "idle",
      subtitleStatus: "stopped",
      phase: "listening",
    })).toMatchObject({
      className: "mode-listening",
      label: "语音助手聆听中",
    });

    expect(getStatusModePresentation({
      activeTab: "assistant",
      meetingStatus: "idle",
      subtitleStatus: "stopped",
      phase: "stopped",
    })).toMatchObject({
      className: "mode-stopped",
      label: "语音助手已停止",
    });
  });

  it("uses meeting status instead of a stale assistant phase", () => {
    expect(getStatusModePresentation({
      activeTab: "meeting",
      meetingStatus: "idle",
      subtitleStatus: "connected",
      phase: "listening",
    })).toMatchObject({
      className: "mode-meeting",
      label: "会议助手待命",
    });

    expect(getStatusModePresentation({
      activeTab: "meeting",
      meetingStatus: "recording",
      subtitleStatus: "connected",
      phase: "stopped",
    })).toMatchObject({
      className: "mode-meeting",
      label: "会议录制中",
    });

    expect(getStatusModePresentation({
      activeTab: "assistant",
      meetingStatus: "finalizing",
      subtitleStatus: "connected",
      phase: "listening",
    })).toMatchObject({
      className: "mode-meeting",
      label: "会议封存中",
    });
  });

  it("uses subtitle status instead of a stale assistant phase", () => {
    expect(getStatusModePresentation({
      activeTab: "subtitles",
      meetingStatus: "idle",
      subtitleStatus: "connected",
      phase: "stopped",
    })).toMatchObject({
      className: "mode-subtitles",
      label: "实时字幕运行中",
    });

    expect(getStatusModePresentation({
      activeTab: "subtitles",
      meetingStatus: "idle",
      subtitleStatus: "stopped",
      phase: "listening",
    })).toMatchObject({
      className: "mode-stopped",
      label: "实时字幕已停止",
    });
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
    pendingTab: "assistant" | "meeting" | "subtitles" | null,
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

  it("shows an inline pending indicator without adding a second status row", () => {
    renderStatusBar("subtitles", false);

    const buttons = Array.from(container.querySelectorAll("button"));
    const assistantButton = buttons.find((button) => button.textContent?.includes("语音助手"));
    const meetingButton = buttons.find((button) => button.textContent?.includes("会议助手"));
    const subtitlesButton = buttons.find((button) => button.textContent?.includes("实时字幕"));

    expect(assistantButton?.disabled).toBe(true);
    expect(subtitlesButton?.disabled).toBe(true);
    expect(meetingButton?.disabled).toBe(false);
    expect(subtitlesButton?.classList.contains("pending")).toBe(true);
    expect(subtitlesButton?.getAttribute("aria-busy")).toBe("true");
    expect(container.querySelector(".workspace-switch-state")).toBeNull();
  });

  it("does not render a visible switch status row when idle", () => {
    renderStatusBar(null, false);

    expect(container.querySelector(".workspace-switch-state")).toBeNull();
    expect(container.querySelector(".status-switch-announcement")).toBeNull();
  });

  it("shows reconcile and explicit failure states", () => {
    renderStatusBar("subtitles", true);
    expect(container.querySelector(".status-switch-announcement")?.textContent).toContain("正在对账");

    renderStatusBar(null, false, "模式已被占用");
    expect(container.querySelector(".status-tab-btn.switch-error")).not.toBeNull();
    expect(container.querySelector("[role='alert']")?.textContent).toContain("模式已被占用");
  });

  it("renders recording status chip without elapsed time timer in meeting tab and disables other tabs cleanly", () => {
    act(() => {
      useMeetingStore.setState({ status: "recording" });
    });
    renderStatusBar(null, false);

    const buttons = Array.from(container.querySelectorAll("button"));
    const assistantButton = buttons.find((button) => button.textContent?.includes("语音助手"));
    const meetingButton = buttons.find((button) => button.textContent?.includes("会议助手"));
    const subtitlesButton = buttons.find((button) => button.textContent?.includes("实时字幕"));

    const recordingChip = meetingButton?.querySelector(".tab-status-chip.recording");
    expect(recordingChip).not.toBeNull();
    expect(recordingChip?.textContent?.trim()).toBe("录制中");

    // Assistant tab shows suspended chip and is disabled
    expect(assistantButton?.querySelector(".tab-status-chip.suspended")).not.toBeNull();
    expect(assistantButton?.disabled).toBe(true);

    // Subtitles tab has no misleading sync chip and is disabled
    expect(subtitlesButton?.querySelector(".tab-status-chip")).toBeNull();
    expect(subtitlesButton?.disabled).toBe(true);

    // Meeting tab is active/clickable
    expect(meetingButton?.disabled).toBe(false);

    act(() => {
      useMeetingStore.setState({ status: "idle" });
    });
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

  async function renderServices(
    services: readonly object[],
    mode: RuntimeStateSnapshot["mode"] = "assistant",
    networkScope: "local" | "network" = "local",
  ): Promise<void> {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ services, network_scope: networkScope }),
    }));

    await act(async () => {
      root.render(createElement(StatusBar, {
        commandSocket: { ...commandSocket, snapshot: { ...assistantSnapshot, mode } },
      }));
    });

    const healthButton = Array.from(container.querySelectorAll("button"))
      .find((button) => button.title.includes("当前模式的服务健康状态"));
    expect(healthButton).toBeDefined();
    act(() => {
      healthButton?.click();
    });
  }

  function findServiceRow(name: string): HTMLDivElement | undefined {
    return Array.from(container.querySelectorAll<HTMLDivElement>(".health-popover-row"))
      .find((row) => row.textContent?.includes(name));
  }

  it("keeps SpeechRail diagnostics in a compact tooltip without changing the process light", async () => {
    await renderServices([
      {
        name: "speechrail",
        status: "ok",
        url: "http://127.0.0.1:8201/health",
        workload: "degraded",
        ws_state: "reconnecting",
        reconnect_count: 3,
        last_event_age_ms: 12500,
        dropped_chunks: 4,
        gap_count: 2,
      },
    ], "subtitles");

    const row = findServiceRow("SpeechRail ASR");
    expect(row?.textContent).toContain("运行正常");
    expect(row?.textContent).not.toContain("语音工作负载：degraded");
    expect(row?.textContent).not.toContain("WebSocket 状态：reconnecting");
    expect(row?.textContent).not.toContain("重连次数：3");
    expect(row?.getAttribute("title")).toContain("语音工作负载：degraded");
    expect(row?.getAttribute("title")).toContain("WebSocket 状态：reconnecting");
    expect(row?.getAttribute("title")).toContain("重连次数：3");
    expect(row?.querySelector(".light-dot")?.classList.contains("state-normal")).toBe(true);
  });

  it("shows a long last-event age as an unclassified raw value", async () => {
    await renderServices([
      {
        name: "speechrail",
        status: "ok",
        url: "http://127.0.0.1:8201/health",
        workload: "degraded",
        last_event_age_ms: 987654,
      },
    ], "subtitles");

    const row = findServiceRow("SpeechRail ASR");
    expect(row?.getAttribute("title")).toContain("距最近事件：987654 ms");
    expect(row?.textContent).not.toContain("关闭游戏");
    expect(row?.textContent).not.toContain("CPU");
    expect(row?.textContent).not.toContain("GPU");
  });

  it("uses network-aware footer copy when services are LAN-accessible", async () => {
    await renderServices([
      { name: "speechrail", status: "ok", url: "http://192.168.1.20:8201/health" },
    ], "subtitles", "network");

    const footer = container.querySelector<HTMLElement>(".health-footer-tip");
    expect(footer?.textContent).toContain("服务可通过局域网访问");
    expect(footer?.textContent).toContain("数据可能在局域网内传输");
    expect(footer?.textContent).not.toContain("数据不出本机");
  });

  it("keeps rendering the legacy three-service response", async () => {
    await renderServices([
      { name: "speechrail", status: "ok", url: "http://127.0.0.1:8201/health" },
      { name: "tts", status: "timeout", url: "http://127.0.0.1:8201/health" },
      { name: "lm", status: "unreachable", url: "http://127.0.0.1:1234" },
    ]);

    expect(findServiceRow("SpeechRail ASR")?.textContent).toContain("当前模式非必需");
    expect(findServiceRow("SpeechRail TTS")?.textContent).toContain("必须组件异常");
    expect(findServiceRow("LM Studio")?.textContent).toContain("必须组件异常");
    expect(findServiceRow("SpeechRail TTS")?.getAttribute("title")).toContain("连接超时");
    expect(findServiceRow("LM Studio")?.getAttribute("title")).toContain("服务未启动");
    expect(container.querySelectorAll(".health-popover-row")).toHaveLength(7);
  });

  it("renders null and unknown workload states defensively", async () => {
    await renderServices([
      {
        name: "speechrail",
        status: "ok",
        url: "http://127.0.0.1:8201/health",
        workload: null,
        ws_state: null,
      },
      {
        name: "speechrail-future",
        status: "ok",
        url: "http://127.0.0.1:8002",
        workload: "future-workload",
        ws_state: "future-ws-state",
      },
    ], "subtitles");

    const speechrailRow = findServiceRow("SpeechRail ASR");
    expect(speechrailRow?.getAttribute("title")).toContain("语音工作负载：未知");
    expect(speechrailRow?.getAttribute("title")).toContain("WebSocket 状态：未知");

    const futureRow = findServiceRow("speechrail-future");
    expect(futureRow?.getAttribute("title")).toContain("语音工作负载：future-workload");
    expect(futureRow?.getAttribute("title")).toContain("WebSocket 状态：future-ws-state");
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
          { name: "speechrail", status: "ok", url: "http://127.0.0.1:8201/health" },
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
    expect(healthButton.textContent).toContain("系统正常 (4/4)");
  });

  it.each(["subtitles", "meeting"] as const)(
    "excludes a stopped pipeline from %s aggregate health",
    async (mode) => {
      const healthButton = await renderHealth(mode, "stopped", "connected", "assistant");

      expect(healthButton.classList.contains("all-ok")).toBe(true);
      expect(healthButton.textContent).toContain(mode === "subtitles" ? "系统正常 (3/3)" : "系统正常 (5/5)");
    },
  );

  it.each([
    ["assistant", "error", "paused", "核心组件异常 (3/4)"],
    ["subtitles", "stopped", "error", "核心组件异常 (2/3)"],
  ] as const)("marks required %s workload errors in aggregate health", async (
    mode,
    pipelineStatus,
    subtitleStatus,
    expectedLabel,
  ) => {
    const healthButton = await renderHealth(mode, pipelineStatus, subtitleStatus, "meeting");

    expect(healthButton.classList.contains("has-error")).toBe(true);
    expect(healthButton.textContent).toContain(expectedLabel);
  });

  it("renders non-required services as neutral even when they are unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        services: [
          { name: "speechrail", status: "unreachable", url: "http://127.0.0.1:8201/health" },
          { name: "tts", status: "ok", url: "http://127.0.0.1:8765" },
          { name: "lm", status: "ok", url: "http://127.0.0.1:1234" },
        ],
      }),
    }));

    const healthButton = await renderHealth("assistant", "running", "paused", "assistant");
    act(() => {
      healthButton.click();
    });

    const speechrailRow = Array.from(container.querySelectorAll<HTMLDivElement>(".health-popover-row"))
      .find((row) => row.textContent?.includes("SpeechRail ASR"));
    expect(healthButton.classList.contains("all-ok")).toBe(true);
    expect(speechrailRow?.textContent).toContain("当前模式非必需");
    expect(speechrailRow?.classList.contains("state-not-required")).toBe(true);
  });

  it("marks required rows and keeps optional rows visually separate", async () => {
    const healthButton = await renderHealth("assistant", "error", "paused", "assistant");
    act(() => {
      healthButton.click();
    });

    const pipelineRow = Array.from(container.querySelectorAll<HTMLDivElement>(".health-popover-row"))
      .find((row) => row.textContent?.includes("交互管道"));
    const subtitleRow = Array.from(container.querySelectorAll<HTMLDivElement>(".health-popover-row"))
      .find((row) => row.textContent?.includes("字幕代理"));
    expect(pipelineRow?.textContent).toContain("必须组件异常");
    expect(pipelineRow?.classList.contains("state-required-error")).toBe(true);
    expect(subtitleRow?.textContent).toContain("当前模式非必需");
    expect(subtitleRow?.classList.contains("state-not-required")).toBe(true);
  });
});
