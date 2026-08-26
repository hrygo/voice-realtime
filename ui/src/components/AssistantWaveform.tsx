import type { AssistantPhase } from "../stores/assistantStore";
import { UnifiedAcousticWaveform } from "./UnifiedAcousticWaveform";
import "./AssistantStatus.css";

/**
 * 语音助手声学波形：接入全局真实声音大小能量流，
 * 动态根据音量大小调节波纹起伏，无声时优雅平缓。
 */
export function AssistantWaveform({
  phase,
  isMuted,
  activeTextTrigger,
}: {
  readonly phase: AssistantPhase;
  readonly isMuted: boolean;
  readonly activeTextTrigger?: unknown;
}) {
  return (
    <UnifiedAcousticWaveform
      themePreset="assistant"
      state={phase}
      isMuted={isMuted}
      activeTextTrigger={activeTextTrigger}
      className="assistant-waveform-canvas"
      ariaLabel="语音助手动态拟真声学波形"
    />
  );
}



