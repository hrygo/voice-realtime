import { beforeEach, describe, expect, it } from "vitest";

import type { PCMOwner } from "../contracts/meetingContract";
import type { RuntimeStateSnapshot } from "../protocol";
import { useUISettingsStore } from "./uiSettingsStore";

describe("useUISettingsStore.applyRuntimeState", () => {
  beforeEach(() => {
    useUISettingsStore.setState({
      mode: "assistant",
      activeMeetingId: null,
      serverSynchronized: false,
    });
  });

  it("accepts the authoritative subtitles runtime mode", () => {
    const owner: PCMOwner = "subtitles";
    const state: RuntimeStateSnapshot = {
      mode: "subtitles",
      pcm_owner: owner,
      runtime_revision: 12,
      active_meeting_id: null,
      meeting_state: null,
      meeting_started_at: null,
      pipeline: "stopped",
      subtitle: "connected",
      storage: "ok",
      mic_muted: false,
      persona: "字幕模式",
      voice: "warm",
      duplex_mode: "speaker_focus",
      session_started_at: null,
    };

    useUISettingsStore.getState().applyRuntimeState(state);

    expect(useUISettingsStore.getState()).toMatchObject({
      mode: "subtitles",
      activeMeetingId: null,
      serverSynchronized: true,
      persona: "字幕模式",
    });
  });
});
