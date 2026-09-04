import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PCMOwner } from "../contracts/meetingContract";
import type { RuntimeStateSnapshot } from "../protocol";
import {
  applyTheme,
  initThemeListener,
  resolveEffectiveTheme,
  useUISettingsStore,
} from "./uiSettingsStore";

describe("Theme management and system mode resolution", () => {
  let mediaQueryListeners: Array<(e: MediaQueryListEvent) => void> = [];
  let matchesDark = false;

  beforeEach(() => {
    mediaQueryListeners = [];
    matchesDark = false;
    document.documentElement.removeAttribute("data-theme");
    delete document.documentElement.dataset.themeSetting;

    vi.spyOn(window, "matchMedia").mockImplementation((query: string) => {
      return {
        matches: matchesDark,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn((event: string, listener: (e: MediaQueryListEvent) => void) => {
          if (event === "change") mediaQueryListeners.push(listener);
        }),
        removeEventListener: vi.fn((event: string, listener: (e: MediaQueryListEvent) => void) => {
          if (event === "change") {
            mediaQueryListeners = mediaQueryListeners.filter((l) => l !== listener);
          }
        }),
        dispatchEvent: vi.fn(),
      } as unknown as MediaQueryList;
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("resolves system mode to light when system does not prefer dark", () => {
    matchesDark = false;
    expect(resolveEffectiveTheme("system")).toBe("light");
    applyTheme("system");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.dataset.themeSetting).toBe("system");
  });

  it("resolves system mode to dark when system prefers dark", () => {
    matchesDark = true;
    expect(resolveEffectiveTheme("system")).toBe("dark");
    applyTheme("system");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.documentElement.dataset.themeSetting).toBe("system");
  });

  it("applies explicit light and dark themes directly", () => {
    applyTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.dataset.themeSetting).toBe("light");

    applyTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.documentElement.dataset.themeSetting).toBe("dark");
  });

  it("automatically reacts to system preference changes when in system mode", () => {
    useUISettingsStore.setState({ theme: "system" });
    matchesDark = false;
    const cleanup = initThemeListener();

    applyTheme("system");
    expect(document.documentElement.dataset.theme).toBe("light");

    // 模拟系统切到暗色
    matchesDark = true;
    for (const listener of mediaQueryListeners) {
      listener({ matches: true } as MediaQueryListEvent);
    }
    expect(document.documentElement.dataset.theme).toBe("dark");

    cleanup();
  });
});

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
