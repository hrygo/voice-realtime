import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App, { type WorkspaceTab } from "./App";
import { useMeetingStore } from "./stores/meetingStore";

const commandSocket = vi.hoisted(() => ({
  ready: true,
  sendCommand: vi.fn().mockResolvedValue({}),
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
    onTabChange,
  }: {
    activeTab: WorkspaceTab;
    onTabChange: (tab: WorkspaceTab) => void;
  }) => (
    <nav data-testid="workspace-tabs" data-active-tab={activeTab}>
      <button type="button" onClick={() => onTabChange("assistant")}>语音助手</button>
      <button type="button" onClick={() => onTabChange("meeting")}>会议助手</button>
      <button type="button" onClick={() => onTabChange("subtitles")}>实时字幕</button>
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

describe("App workspace refresh state", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: createMemoryStorage(),
    });
    window.localStorage.clear();
    commandSocket.sendCommand.mockClear();
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

  it("restores the last workspace without sending runtime commands", () => {
    window.localStorage.setItem("voice-studio:workspace-tab", "subtitles");

    act(() => {
      root.render(<App />);
    });

    expect(container.querySelector("[data-testid='subtitles-panel']")).not.toBeNull();
    expect(commandSocket.sendCommand).not.toHaveBeenCalled();
  });

  it("persists explicit workspace navigation for the next refresh", () => {
    act(() => {
      root.render(<App />);
    });

    const meetingButton = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "会议助手",
    );
    expect(meetingButton).not.toBeUndefined();

    act(() => {
      meetingButton?.click();
    });

    expect(window.localStorage.getItem("voice-studio:workspace-tab")).toBe("meeting");
  });

  it("reconciles an active meeting to the meeting workspace without side effects", () => {
    window.localStorage.setItem("voice-studio:workspace-tab", "subtitles");
    useMeetingStore.setState({ status: "recording" });

    act(() => {
      root.render(<App />);
    });

    expect(container.querySelector("[data-testid='meeting-panel']")).not.toBeNull();
    expect(commandSocket.sendCommand).not.toHaveBeenCalled();
  });
});
