import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App, { resolveWorkspaceTab, type WorkspaceTab } from "./App";
import type { RuntimeStateSnapshot } from "./protocol";
import { useMeetingStore } from "./stores/meetingStore";

const commandSocket = vi.hoisted(() => ({
  state: "open" as const,
  ready: true,
  snapshot: null as RuntimeStateSnapshot | null,
  highestRuntimeRevision: null as number | null,
  sendCommand: vi.fn(),
  reconcileRuntime: vi.fn(),
}));

vi.mock("./hooks/useCommandSocket", () => ({
  useCommandSocket: () => commandSocket,
}));

vi.mock("./hooks/useMeetingSocket", () => ({
  useMeetingSocket: () => undefined,
}));

vi.mock("./components/StatusBar", () => ({
  default: ({
    activeTab,
    pendingTab,
    reconciling,
    switchError,
    onTabChange,
  }: {
    activeTab: WorkspaceTab | null;
    pendingTab: WorkspaceTab | null;
    reconciling: boolean;
    switchError: string | null;
    onTabChange: (tab: WorkspaceTab) => void;
  }) => (
    <nav
      data-testid="workspace-tabs"
      data-active-tab={activeTab ?? ""}
      data-pending-tab={pendingTab ?? ""}
    >
      <button type="button" onClick={() => onTabChange("assistant")}>语音助手</button>
      <button type="button" onClick={() => onTabChange("meeting")}>会议助手</button>
      <button type="button" onClick={() => onTabChange("subtitles")}>实时字幕</button>
      {pendingTab && <span>{reconciling ? "正在对账" : "切换中"}</span>}
      {switchError && <span role="alert">{switchError}</span>}
    </nav>
  ),
}));

vi.mock("./components/AssistantPanel", () => ({
  default: () => <div data-testid="assistant-panel" />,
}));

vi.mock("./components/meeting/MeetingPanel", () => ({
  default: () => <div data-testid="meeting-panel" />,
}));

vi.mock("./components/SubtitleStream", () => ({
  default: () => <div data-testid="subtitles-panel" />,
}));

vi.mock("./components/ShortcutsModal", () => ({
  default: () => null,
}));

vi.mock("./components/Toast", () => ({
  ToastContainer: () => null,
  showToast: vi.fn(),
}));

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

function createMemoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => Array.from(values.keys())[index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, value),
  };
}

function runtimeState(
  mode: RuntimeStateSnapshot["mode"],
  runtimeRevision: number,
): RuntimeStateSnapshot {
  return {
    mode,
    pcm_owner: mode === "idle" ? "none" : mode,
    pipeline: mode === "assistant" ? "listening" : "idle",
    subtitle: mode === "subtitles" ? "connected" : "idle",
    mic_muted: false,
    runtime_revision: runtimeRevision,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function commandError(code: string, message: string): Error & { code: string } {
  return Object.assign(new Error(message), { code });
}

describe("resolveWorkspaceTab", () => {
  it("forces meeting and subtitles modes to their authoritative workspaces", () => {
    expect(resolveWorkspaceTab("meeting", "assistant", "assistant")).toBe("meeting");
    expect(resolveWorkspaceTab("subtitles", "meeting", "meeting")).toBe("subtitles");
  });

  it("falls back from stored subtitles for assistant and idle modes", () => {
    expect(resolveWorkspaceTab("assistant", "subtitles", null)).toBe("assistant");
    expect(resolveWorkspaceTab("idle", "subtitles", "subtitles")).toBe("assistant");
  });

  it("preserves meeting history navigation outside meeting mode", () => {
    expect(resolveWorkspaceTab("assistant", "assistant", "meeting")).toBe("meeting");
    expect(resolveWorkspaceTab("idle", "meeting", null)).toBe("meeting");
  });
});

describe("App authoritative workspace state", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: createMemoryStorage(),
    });
    window.localStorage.clear();
    commandSocket.ready = true;
    commandSocket.snapshot = null;
    commandSocket.highestRuntimeRevision = null;
    commandSocket.sendCommand.mockReset();
    commandSocket.reconcileRuntime.mockReset();
    useMeetingStore.getState().resetActiveSession();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  function renderApp(): void {
    act(() => {
      root.render(<App />);
    });
  }

  function setAuthoritativeState(mode: RuntimeStateSnapshot["mode"], revision: number): void {
    commandSocket.snapshot = runtimeState(mode, revision);
    commandSocket.highestRuntimeRevision = revision;
    renderApp();
  }

  function clickTab(label: string): void {
    const button = Array.from(container.querySelectorAll("button")).find(
      (candidate) => candidate.textContent === label,
    );
    expect(button).not.toBeUndefined();
    act(() => {
      button?.click();
    });
  }

  it("does not mount subtitles or send commands before the first authoritative snapshot", () => {
    window.localStorage.setItem("voice-studio:workspace-tab", "subtitles");

    renderApp();

    expect(container.querySelector("[data-testid='assistant-panel']")).toBeNull();
    expect(container.querySelector("[data-testid='subtitles-panel']")).toBeNull();
    expect(commandSocket.sendCommand).not.toHaveBeenCalled();
  });

  it.each([
    ["meeting", "assistant", "meeting-panel"],
    ["subtitles", "assistant", "subtitles-panel"],
    ["assistant", "subtitles", "assistant-panel"],
    ["idle", "subtitles", "assistant-panel"],
    ["assistant", "meeting", "meeting-panel"],
    ["idle", "meeting", "meeting-panel"],
  ] as const)(
    "resolves mode %s with stored %s to %s",
    (mode, storedTab, panelTestId) => {
      window.localStorage.setItem("voice-studio:workspace-tab", storedTab);

      setAuthoritativeState(mode, 1);

      expect(container.querySelector(`[data-testid='${panelTestId}']`)).not.toBeNull();
      expect(commandSocket.sendCommand).not.toHaveBeenCalled();
    },
  );

  it("navigates to meeting history without changing runtime mode", () => {
    setAuthoritativeState("assistant", 3);

    clickTab("会议助手");

    expect(container.querySelector("[data-testid='meeting-panel']")).not.toBeNull();
    expect(window.localStorage.getItem("voice-studio:workspace-tab")).toBe("meeting");
    expect(commandSocket.sendCommand).not.toHaveBeenCalled();
  });

  it("keeps the current workspace pending until a matching subtitles ack and deduplicates clicks", async () => {
    const acknowledgement = deferred<RuntimeStateSnapshot>();
    commandSocket.sendCommand.mockReturnValue(acknowledgement.promise);
    setAuthoritativeState("assistant", 4);

    clickTab("实时字幕");
    clickTab("实时字幕");

    expect(container.querySelector("[data-testid='assistant-panel']")).not.toBeNull();
    expect(container.querySelector("[data-testid='subtitles-panel']")).toBeNull();
    expect(container.textContent).toContain("切换中");
    expect(commandSocket.sendCommand).toHaveBeenCalledTimes(1);
    expect(commandSocket.sendCommand).toHaveBeenCalledWith({ cmd: "start_subtitles" });

    await act(async () => {
      acknowledgement.resolve(runtimeState("subtitles", 5));
      await acknowledgement.promise;
    });

    expect(container.querySelector("[data-testid='subtitles-panel']")).not.toBeNull();
    expect(window.localStorage.getItem("voice-studio:workspace-tab")).toBe("subtitles");
  });

  it("keeps subtitles mounted until a matching assistant ack", async () => {
    const acknowledgement = deferred<RuntimeStateSnapshot>();
    commandSocket.sendCommand.mockReturnValue(acknowledgement.promise);
    setAuthoritativeState("subtitles", 7);

    clickTab("语音助手");

    expect(container.querySelector("[data-testid='subtitles-panel']")).not.toBeNull();
    expect(commandSocket.sendCommand).toHaveBeenCalledWith({ cmd: "start_assistant" });

    await act(async () => {
      acknowledgement.resolve(runtimeState("assistant", 8));
      await acknowledgement.promise;
    });

    expect(container.querySelector("[data-testid='assistant-panel']")).not.toBeNull();
  });

  it("keeps the current workspace and reports an explicit switch failure", async () => {
    commandSocket.sendCommand.mockRejectedValue(commandError("mode_conflict", "模式已被占用"));
    setAuthoritativeState("assistant", 1);

    clickTab("实时字幕");
    await act(async () => {
      await Promise.resolve();
    });

    expect(container.querySelector("[data-testid='assistant-panel']")).not.toBeNull();
    expect(container.querySelector("[role='alert']")?.textContent).toContain("模式已被占用");
  });

  it("reconciles a timeout and commits when a higher target broadcast arrives", async () => {
    commandSocket.sendCommand.mockRejectedValue(commandError("timeout", "控制指令确认超时"));
    commandSocket.reconcileRuntime.mockReturnValue(new Promise<RuntimeStateSnapshot>(() => {}));
    setAuthoritativeState("assistant", 10);

    clickTab("实时字幕");
    await act(async () => {
      await Promise.resolve();
    });

    expect(container.querySelector("[data-testid='assistant-panel']")).not.toBeNull();
    expect(container.textContent).toContain("正在对账");
    expect(commandSocket.reconcileRuntime).toHaveBeenCalledTimes(1);

    setAuthoritativeState("subtitles", 11);

    expect(container.querySelector("[data-testid='subtitles-panel']")).not.toBeNull();
    expect(container.textContent).not.toContain("正在对账");
  });

  it("lets a higher conflicting revision replace a pending switch", () => {
    commandSocket.sendCommand.mockReturnValue(new Promise<RuntimeStateSnapshot>(() => {}));
    setAuthoritativeState("assistant", 20);

    clickTab("实时字幕");
    setAuthoritativeState("meeting", 21);

    expect(container.querySelector("[data-testid='meeting-panel']")).not.toBeNull();
    expect(container.querySelector("[data-testid='workspace-tabs']")?.getAttribute("data-pending-tab"))
      .toBe("");
  });
});
