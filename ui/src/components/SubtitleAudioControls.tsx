import { useEffect, useRef, useState } from "react";
import type { CommandSocketApi } from "../hooks/useCommandSocket";
import { apiUrl } from "../config/runtimeConfig";
import { isRecord, type SubtitleCaptureSelection } from "../protocol";
import "./SubtitleAudioControls.css";

interface OutputDevice {
  device_ref: string;
  label: string;
  is_default: boolean;
}

export default function SubtitleAudioControls({ channel }: { channel: CommandSocketApi }) {
  const snapshot = channel.snapshot;
  const enabled = snapshot?.capabilities?.physical_output_enabled === true;
  const active = snapshot?.mode === "subtitles" && snapshot.pcm_owner === "subtitles";
  const [source, setSource] = useState<"microphone" | "physical_output">("microphone");
  const [devices, setDevices] = useState<OutputDevice[]>([]);
  const [deviceRef, setDeviceRef] = useState("");
  const [pending, setPending] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const currentSelection = snapshot?.subtitle_capture;

  useEffect(() => {
    if (currentSelection) {
      setSource(currentSelection.source);
      setDeviceRef(currentSelection.device_ref ?? "");
    }
  }, [currentSelection?.source, currentSelection?.device_ref]);

  useEffect(() => () => request.current?.abort(), []);

  async function refreshDevices() {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(apiUrl("/api/audio/output-devices"), { signal: controller.signal });
      const body: unknown = await response.json();
      if (!response.ok) throw new Error(isRecord(body) && typeof body.detail === "string" ? body.detail : "输出设备不可用");
      if (!isRecord(body) || !Array.isArray(body.devices) || body.devices.length > 128) {
        throw new Error("输出设备列表无效");
      }
      const parsed = body.devices.map((device: unknown): OutputDevice => {
        if (!isRecord(device) || typeof device.device_ref !== "string"
          || !/^vrdev1_[A-Za-z0-9_-]{43}$/.test(device.device_ref)
          || typeof device.label !== "string" || device.label.length > 128
          || typeof device.is_default !== "boolean") throw new Error("输出设备列表无效");
        return { device_ref: device.device_ref, label: device.label, is_default: device.is_default };
      });
      if (controller.signal.aborted) return;
      setDevices(parsed);
      setDeviceRef((previous) => parsed.some((item) => item.device_ref === previous)
        ? previous : (parsed.find((item) => item.is_default) ?? parsed[0])?.device_ref ?? "");
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "输出设备不可用");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  async function run() {
    setPending(true);
    setError(null);
    try {
      if (active) {
        await channel.sendCommand({ cmd: "stop_active_mode" });
      } else {
        const capture: SubtitleCaptureSelection = source === "physical_output"
          ? { source, device_ref: deviceRef } : { source };
        await channel.sendCommand({ cmd: "start_subtitles", capture });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "音频来源操作失败");
      await channel.reconcileRuntime().catch(() => {});
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="subtitle-audio-controls" aria-label="字幕音频来源">
      <label htmlFor="subtitle-audio-source">音频来源</label>
      <select id="subtitle-audio-source" value={source} disabled={active || pending || !channel.ready}
        onChange={(event) => setSource(event.target.value as "microphone" | "physical_output")}>
        <option value="microphone">麦克风</option>
        <option value="physical_output" disabled={!enabled}>电脑声音{enabled ? "" : "（尚未启用）"}</option>
      </select>
      {source === "physical_output" && <>
        <label htmlFor="subtitle-output-device">输出设备</label>
        <select id="subtitle-output-device" value={deviceRef} disabled={active || pending || loading}
          onChange={(event) => setDeviceRef(event.target.value)}>
          <option value="">请选择输出设备</option>
          {deviceRef && !devices.some((device) => device.device_ref === deviceRef)
            && <option value={deviceRef}>本次已选输出设备</option>}
          {devices.map((device) => <option key={device.device_ref} value={device.device_ref}>
            {device.label}{device.is_default ? "（系统默认）" : ""}
          </option>)}
        </select>
        <button type="button" className="btn-ctrl" onClick={() => void refreshDevices()}
          disabled={pending || loading || active}>{loading ? "正在读取…" : "刷新输出设备"}</button>
        <p>采集所选设备播放的所有应用声音，不包含麦克风。本次锁定设备；更换输出后请停止并重新选择。</p>
      </>}
      <p role="status">{pending ? "正在处理；首次采集电脑声音时请完成系统授权…"
        : snapshot?.output_capture_active ? "正在采集电脑声音"
        : snapshot?.output_capture_error && source === "physical_output" ? "电脑声音采集已中断"
        : active ? "字幕已启动；停止后可切换来源" : "字幕已停止"}</p>
      {(error || snapshot?.output_capture_error) && <p role="alert">{error ?? snapshot?.output_capture_error}</p>}
      <button type="button" className="btn-ctrl" onClick={() => void run()}
        disabled={!channel.ready || pending || snapshot?.mode === "meeting"
          || (!active && source === "physical_output" && (!enabled || !deviceRef))}>
        {active ? "停止字幕采集" : "开始字幕采集"}
      </button>
    </section>
  );
}
