import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandSocketApi } from "../../hooks/useCommandSocket";
import { useMeetingStore } from "../../stores/meetingStore";
import { useUISettingsStore } from "../../stores/uiSettingsStore";
import MeetingPanel from "./MeetingPanel";

vi.mock("../../services/meetingApi", () => ({
  meetingApi: {
    fetchMeetings: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
  },
}));

vi.mock("./MeetingWaveform", () => ({
  MeetingWaveform: ({ isMuted }: { isMuted?: boolean }) => (
    <div data-testid="meeting-waveform" data-muted={String(Boolean(isMuted))} />
  ),
}));

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const sendCommandMock = vi.fn();

const commandSocket = {
  state: "open" as const,
  ready: true,
  snapshot: null,
  highestRuntimeRevision: null,
  sendCommand: sendCommandMock,
  reconcileRuntime: vi.fn(),
} as unknown as CommandSocketApi;

describe("MeetingPanel microphone state projection", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    const meetingState = useMeetingStore.getState();
    useMeetingStore.setState({
      activeMeetingId: "meeting-1",
      activeMeeting: null,
      status: "recording",
      selectedMeetingId: null,
      selectedMeeting: null,
      sessionStartedAt: "2026-08-27T10:00:00.000Z",
      health: { ...meetingState.health, mic_muted: false },
    });
    useUISettingsStore.setState({ micMuted: true });

    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    useMeetingStore.getState().resetActiveSession();
    useUISettingsStore.setState({ micMuted: false });
  });

  it("uses the control-plane mute value for both meeting indicators", async () => {
    await act(async () => {
      root.render(<MeetingPanel commandSocket={commandSocket} />);
      await Promise.resolve();
    });

    const muteBadge = container.querySelector(".pinned-muted-badge");
    expect(muteBadge).not.toBeNull();
    expect(muteBadge?.textContent).toContain("静音");
    expect(container.querySelector(".recording-vu-meter")?.getAttribute("title")).toBe(
      "麦克风已静音 (快捷键 M)",
    );
  });

  it("opens the custom end confirmation before sending the end command", async () => {
    const nativeConfirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    sendCommandMock.mockReset();
    sendCommandMock.mockResolvedValue({});

    try {
      await act(async () => {
        root.render(<MeetingPanel commandSocket={commandSocket} />);
        await Promise.resolve();
      });

      const endButton = container.querySelector(".btn-end-meeting") as HTMLButtonElement;
      expect(endButton).not.toBeNull();

      await act(async () => {
        endButton.click();
        await Promise.resolve();
      });

      expect(nativeConfirm).not.toHaveBeenCalled();
      expect(container.querySelector(".meeting-end-confirm-dialog")).not.toBeNull();
      expect(sendCommandMock).not.toHaveBeenCalled();

      const confirmButton = container.querySelector(
        ".meeting-end-confirm-submit",
      ) as HTMLButtonElement;
      await act(async () => {
        confirmButton.click();
        await Promise.resolve();
      });

      expect(sendCommandMock).toHaveBeenCalledWith({
        cmd: "end_meeting",
        meeting_id: "meeting-1",
        contract_version: "1",
      });
    } finally {
      nativeConfirm.mockRestore();
    }
  });
});
