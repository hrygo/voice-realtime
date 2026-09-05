import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import SubtitleAudioControls from "./SubtitleAudioControls";
import type { CommandSocketApi } from "../hooks/useCommandSocket";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let root: Root;
let container: HTMLDivElement;
let channel: CommandSocketApi;
const deviceRef = "vrdev1_" + "A".repeat(43);

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  channel = {
    state: "open", ready: true, highestRuntimeRevision: 1,
    snapshot: {
      mode: "idle", pcm_owner: "none", pipeline: "stopped", subtitle: "paused",
      mic_muted: false, runtime_revision: 1,
      capabilities: { inner_os_enabled: false, inner_os_analysis_enabled: false,
        inner_os_channel: "loopback_only", physical_output_enabled: true },
    },
    sendCommand: vi.fn().mockResolvedValue({}), reconcileRuntime: vi.fn().mockResolvedValue({}),
  };
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});
function render() { act(() => root.render(<SubtitleAudioControls channel={channel} />)); }
function button(label: string) {
  return Array.from(container.querySelectorAll("button")).find((item) => item.textContent === label)!;
}

it("enumerates only on request and starts the explicitly selected output", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    enabled: true, devices: [{ device_ref: deviceRef, label: "扬声器", is_default: true }],
  }) });
  vi.stubGlobal("fetch", fetch);
  render();
  expect(fetch).not.toHaveBeenCalled();
  const select = container.querySelector("#subtitle-audio-source") as HTMLSelectElement;
  act(() => { select.value = "physical_output"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  expect(button("开始字幕采集").disabled).toBe(true);
  await act(async () => button("刷新输出设备").click());
  expect(container.textContent).toContain("扬声器（系统默认）");
  await act(async () => button("开始字幕采集").click());
  expect(channel.sendCommand).toHaveBeenCalledWith({ cmd: "start_subtitles",
    capture: { source: "physical_output", device_ref: deviceRef } });
});

it("keeps the capture scope visible and permits stopping while output is active", async () => {
  channel = { ...channel, snapshot: { ...channel.snapshot!, mode: "subtitles", pcm_owner: "subtitles",
    subtitle_capture: { source: "physical_output", device_ref: deviceRef }, output_capture_active: true } };
  render();
  expect(container.textContent).toContain("正在采集电脑声音");
  expect((container.querySelector("select") as HTMLSelectElement).disabled).toBe(true);
  await act(async () => button("停止字幕采集").click());
  expect(channel.sendCommand).toHaveBeenCalledWith({ cmd: "stop_active_mode" });
});

it("shows safe permission errors and reconciles the authoritative state", async () => {
  channel = { ...channel, sendCommand: vi.fn().mockRejectedValue(new Error("系统音频权限未授予")) };
  render();
  await act(async () => button("开始字幕采集").click());
  expect(container.querySelector("[role=alert]")?.textContent).toBe("系统音频权限未授予");
  expect(channel.reconcileRuntime).toHaveBeenCalledOnce();
});
