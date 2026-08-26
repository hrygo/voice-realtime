import { UnifiedAcousticWaveform } from "../UnifiedAcousticWaveform";

/**
 * 会议助手专属声学波形：接入全局真实声音大小能量流，
 * 动态根据音量大小调节深海蓝宝与翡翠青波纹起伏，无声时优雅平缓。
 */
export function MeetingWaveform({
  isRecording,
  hasPartial,
  isMuted = false,
  activeTextTrigger,
}: {
  readonly isRecording: boolean;
  readonly hasPartial: boolean;
  readonly isMuted?: boolean;
  readonly activeTextTrigger?: unknown;
}) {
  return (
    <UnifiedAcousticWaveform
      themePreset="meeting"
      state={hasPartial ? "speaking" : (isRecording ? "recording" : "idle")}
      isMuted={isMuted}
      activeTextTrigger={activeTextTrigger}
      className="meeting-waveform-canvas"
      ariaLabel="会议高保真拾音与声纹分轨声学波形"
    />
  );
}


