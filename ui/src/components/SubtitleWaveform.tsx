import { UnifiedAcousticWaveform } from "./UnifiedAcousticWaveform";

/**
 * 实时字幕专属声学波形：接入全局真实声音大小能量流，
 * 动态根据音量大小调节琥珀翡翠波纹起伏，无声时优雅平缓。
 */
export function SubtitleWaveform({
  connected,
  hasPartial,
  isMuted = false,
  activeTextTrigger,
}: {
  readonly connected: boolean;
  readonly hasPartial: boolean;
  readonly isMuted?: boolean;
  readonly activeTextTrigger?: unknown;
}) {
  return (
    <UnifiedAcousticWaveform
      themePreset="subtitle"
      state={hasPartial ? "speaking" : "idle"}
      isMuted={!connected || isMuted}
      activeTextTrigger={activeTextTrigger}
      className="subtitle-waveform-canvas"
      ariaLabel="实时字幕流式声学波形"
    />
  );
}


