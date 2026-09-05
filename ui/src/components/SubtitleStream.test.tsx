import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import SubtitleStream, { getSubtitleListeningPresentation } from "./SubtitleStream";
import { useSubtitleStore } from "../stores/subtitleStore";
import { useUISettingsStore } from "../stores/uiSettingsStore";
import type { CommandSocketApi } from "../hooks/useCommandSocket";

vi.mock("../hooks/useEventSocket", () => ({
  useEventSocket: () => ({ state: "open" }),
}));

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const originalClipboard = navigator.clipboard;
const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;
const originalAnchorClick = HTMLAnchorElement.prototype.click;

const sampleLines = [
  {
    speaker: 0,
    text: "这是一条测试字幕",
    start: "00:00:01",
    end: "00:00:02",
  },
];

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
    useUISettingsStore.setState({ micMuted: false });
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

  afterEach(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: originalClipboard,
    });
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: originalCreateObjectURL,
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: originalRevokeObjectURL,
    });
    Object.defineProperty(HTMLAnchorElement.prototype, "click", {
      configurable: true,
      value: originalAnchorClick,
    });
    vi.restoreAllMocks();
    useUISettingsStore.setState({ micMuted: false });
  });

  function renderWithRuntime(snapshot: NonNullable<CommandSocketApi["snapshot"]>) {
    const commandSocket: CommandSocketApi = {
      state: "open",
      ready: true,
      snapshot,
      highestRuntimeRevision: snapshot.runtime_revision,
      sendCommand: vi.fn().mockResolvedValue(snapshot),
      reconcileRuntime: vi.fn().mockResolvedValue(snapshot),
    };
    act(() => {
      root.render(<SubtitleStream commandSocket={commandSocket} />);
    });
  }

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

  it("shows local feedback after copying all subtitles", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    useSubtitleStore.setState({ lines: sampleLines, rawLines: sampleLines });

    act(() => {
      root.render(<SubtitleStream />);
    });

    const copyButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("复制"),
    ) as HTMLButtonElement;

    await act(async () => {
      copyButton.click();
      await Promise.resolve();
    });

    expect(writeText).toHaveBeenCalledWith("说话人 0: 这是一条测试字幕");
    expect(copyButton.textContent).toContain("已复制");
    expect(container.querySelector('[role="status"]')?.textContent).toContain("已复制");
  });

  it("shows local feedback after starting an SRT download", () => {
    const createObjectURL = vi.fn().mockReturnValue("blob:subtitle");
    const revokeObjectURL = vi.fn();
    const anchorClick = vi.fn();
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: createObjectURL,
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: revokeObjectURL,
    });
    Object.defineProperty(HTMLAnchorElement.prototype, "click", {
      configurable: true,
      value: anchorClick,
    });
    useSubtitleStore.setState({ lines: sampleLines, rawLines: sampleLines });

    act(() => {
      root.render(<SubtitleStream />);
    });

    const srtButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("SRT"),
    ) as HTMLButtonElement;

    act(() => {
      srtButton.click();
    });

    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(anchorClick).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:subtitle");
    expect(srtButton.textContent).toContain("已下载");
  });

  it("does not call a muted microphone an active listening source", () => {
    useUISettingsStore.setState({ micMuted: true });
    useSubtitleStore.setState({ connected: true });
    renderWithRuntime({
      mode: "subtitles",
      pcm_owner: "subtitles",
      pipeline: "stopped",
      subtitle: "connected",
      mic_muted: true,
      runtime_revision: 2,
      subtitle_capture: { source: "microphone" },
    });

    const pill = container.querySelector(".subtitle-listening-pill");
    expect(pill?.textContent).toContain("麦克风已静音");
    expect(pill?.textContent).not.toContain("实时收听中");
    expect(pill?.classList.contains("listening")).toBe(false);
    expect(getSubtitleListeningPresentation({
      connected: true,
      partial: false,
      micMuted: true,
      runtimeMode: "subtitles",
      pcmOwner: "subtitles",
      captureSource: "microphone",
    }).active).toBe(false);
  });

  it("does not mark a selected computer source active before capture starts", () => {
    useSubtitleStore.setState({ connected: true });
    renderWithRuntime({
      mode: "subtitles",
      pcm_owner: "subtitles",
      pipeline: "stopped",
      subtitle: "connected",
      mic_muted: false,
      runtime_revision: 3,
      subtitle_capture: { source: "physical_output", device_ref: "vrdev1_test" },
      output_capture_active: false,
    });

    const pill = container.querySelector(".subtitle-listening-pill");
    expect(pill?.textContent).toContain("电脑音源待采集");
    expect(pill?.classList.contains("listening")).toBe(false);
    expect(getSubtitleListeningPresentation({
      connected: true,
      partial: false,
      runtimeMode: "subtitles",
      pcmOwner: "subtitles",
      captureSource: "physical_output",
      outputCaptureActive: false,
    }).active).toBe(false);
  });

  it("keeps computer audio active when the microphone is muted", () => {
    useUISettingsStore.setState({ micMuted: true });
    useSubtitleStore.setState({ connected: true });
    renderWithRuntime({
      mode: "subtitles",
      pcm_owner: "subtitles",
      pipeline: "stopped",
      subtitle: "connected",
      mic_muted: true,
      runtime_revision: 3,
      subtitle_capture: { source: "physical_output", device_ref: "vrdev1_test" },
      output_capture_active: true,
    });

    const pill = container.querySelector(".subtitle-listening-pill");
    expect(pill?.textContent).toContain("正在采集电脑声音");
    expect(pill?.textContent).not.toContain("停止");
    expect(pill?.classList.contains("listening")).toBe(true);
    expect(getSubtitleListeningPresentation({
      connected: true,
      partial: false,
      micMuted: true,
      runtimeMode: "subtitles",
      pcmOwner: "subtitles",
      captureSource: "physical_output",
      outputCaptureActive: true,
    }).active).toBe(true);
  });

  it("shows that subtitles are not started when no workload owns the input", () => {
    useSubtitleStore.setState({ connected: true });
    renderWithRuntime({
      mode: "idle",
      pcm_owner: "none",
      pipeline: "stopped",
      subtitle: "connected",
      mic_muted: false,
      runtime_revision: 4,
    });

    const pill = container.querySelector(".subtitle-listening-pill");
    expect(pill?.textContent).toContain("字幕未启动");
    expect(pill?.classList.contains("listening")).toBe(false);
    expect(getSubtitleListeningPresentation({
      connected: true,
      partial: false,
      runtimeMode: "idle",
      pcmOwner: "none",
    }).active).toBe(false);
  });
});
