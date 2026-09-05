import type { AssistantPhase, TurnMetrics } from "../stores/assistantStore";
import type { DuplexMode } from "../stores/uiSettingsStore";

export interface VoiceCatalogItem {
  readonly id: string;
  readonly name: string;
  readonly instruction?: string;
  readonly is_system: boolean;
  readonly created_at?: number;
  readonly available?: boolean;
}

export const DEFAULT_SYSTEM_VOICES: readonly VoiceCatalogItem[] = [
  { id: "default", name: "默认原声", instruction: "标准专业、吐字清晰的女声普通话", is_system: true },
  { id: "warm", name: "温暖磁性", instruction: "温和厚重、富有同理心的青年男声", is_system: true },
  { id: "bright", name: "清脆干练", instruction: "清脆活泼、节奏轻快的青年女声", is_system: true },
  { id: "calm", name: "沉稳专业", instruction: "沉稳冷静、节奏从容的专业播音员", is_system: true },
];

export const FALLBACK_VOICES: readonly string[] = ["default", "warm", "bright", "calm"];

export const VOICE_CONFIGS: Record<string, { label: string; tag: string }> = {
  default: { label: "默认原声", tag: "标准" },
  warm: { label: "温暖磁性", tag: "亲和" },
  bright: { label: "清脆干练", tag: "活力" },
  calm: { label: "沉稳专业", tag: "严谨" },
};

export function formatMetric(value: number | null): string {
  return value === null ? "—" : `${value}ms`;
}

export const PHASE_CONFIG: Record<
  AssistantPhase,
  { label: string; icon: string; desc: string; className: string }
> = {
  idle: { label: "待命", icon: "💤", desc: "系统待命就绪", className: "phase-idle" },
  listening: { label: "聆听", icon: "👂", desc: "正在接收麦克风语音，请直接说话...", className: "phase-listening" },
  thinking: { label: "思考", icon: "🧠", desc: "LM Studio 推理生成中...", className: "phase-thinking" },
  speaking: { label: "播报", icon: "🗣️", desc: "Qwen3-TTS 语音播报中...", className: "phase-speaking" },
  degraded: { label: "降级", icon: "⚠️", desc: "交互链路异常，请检查服务状态", className: "phase-idle" },
  stopped: { label: "已停止", icon: "⏹️", desc: "语音交互会话已停止", className: "phase-idle" },
};

export const DUPLEX_MODE_PRESENTATION: Record<
  DuplexMode,
  {
    readonly icon: string;
    readonly label: string;
    readonly summary: string;
    readonly detail: string;
    readonly interruptionEnabled: boolean;
  }
> = {
  speaker_focus: {
    icon: "🔊",
    label: "扬声器",
    summary: "播报时不接收插话",
    detail: "通过扬声器播放；播报期间暂停麦克风输入，避免回声触发新一轮对话。",
    interruptionEnabled: false,
  },
  headphone_duplex: {
    icon: "🎧",
    label: "耳机",
    summary: "播报时可随时插话",
    detail: "通过耳机播放；播报期间保持麦克风监听，检测到真人声音后立即打断。需佩戴耳机。",
    interruptionEnabled: true,
  },
};

export type DuplexModeFeedbackTone = "active" | "switching" | "error" | "offline";

export interface DuplexModeFeedback {
  readonly tone: DuplexModeFeedbackTone;
  readonly title: string;
  readonly detail: string;
}

export function canRequestDuplexModeChange(
  currentMode: DuplexMode,
  requestedMode: DuplexMode,
  pendingMode: DuplexMode | null,
): boolean {
  return currentMode !== requestedMode && pendingMode === null;
}

export function getDuplexToggleMode(
  currentMode: DuplexMode,
  pendingMode: DuplexMode | null,
): DuplexMode {
  return pendingMode ?? currentMode;
}

export function getDuplexModeFeedback(
  currentMode: DuplexMode,
  pendingMode: DuplexMode | null,
  commandReady: boolean,
  errorMessage?: string,
): DuplexModeFeedback {
  if (!commandReady) {
    return {
      tone: "offline",
      title: "正在连接控制端",
      detail: "连接成功后才能切换声音输出与插话方式。",
    };
  }
  if (pendingMode !== null) {
    return {
      tone: "switching",
      title: `正在切换到「${DUPLEX_MODE_PRESENTATION[pendingMode].label}」`,
      detail: "系统正在应用新的声音输出与插话设置，请稍候。",
    };
  }
  if (errorMessage) {
    return {
      tone: "error",
      title: errorMessage,
      detail: `当前仍使用「${DUPLEX_MODE_PRESENTATION[currentMode].label}」。再次选择目标模式可重试。`,
    };
  }
  return {
    tone: "active",
    title: `当前使用「${DUPLEX_MODE_PRESENTATION[currentMode].label}」`,
    detail: DUPLEX_MODE_PRESENTATION[currentMode].detail,
  };
}

export interface TelemetryBadge {
  readonly className: "fast" | "good" | "slow" | "idle";
  readonly label: "首包极速" | "首包良好" | "首包偏高" | "数据不足" | "待命中";
  readonly value: number | null;
}

export interface TelemetryHelpStep {
  readonly title: string;
  readonly formula: string;
  readonly event: string;
  readonly description: string;
}

export const TELEMETRY_HELP_STEPS: readonly TelemetryHelpStep[] = [
  {
    title: "转写等待",
    formula: "max(0, STT final − 断句完成)",
    event: "UserStoppedSpeakingFrame → TranscriptionFrame",
    description: "从断句完成到语音转写产出最终文本帧的等待时间。若 final 已先于断句帧到达，则显示 0ms；这不代表模型识别耗时为 0，也不包含用户说话持续时间。",
  },
  {
    title: "LLM 首字",
    formula: "LLM 首字 − max(STT final, 断句完成)",
    event: "TranscriptionFrame / UserStoppedSpeakingFrame → LLMTextFrame",
    description: "从转写完成与断句完成两者较晚者，到大模型输出第一段文本的等待时间；不代表完整回答生成完成。",
  },
  {
    title: "TTS 首包",
    formula: "TTS 首帧 − LLM 首字",
    event: "LLMTextFrame → TTSAudioRawFrame",
    description: "大模型开始输出文本后，到语音合成送出第一帧音频的等待时间；这是首个语音包进入交互管道，不代表设备扬声器已经发声或整段语音播放完。",
  },
] as const;

type TelemetryMetrics = Pick<TurnMetrics, "sttMs" | "llmTtftMs" | "ttsTtfbMs" | "e2eMs">;

export function getTelemetryBadge(metrics: TelemetryMetrics | null): TelemetryBadge {
  if (metrics === null) return { className: "idle", label: "待命中", value: null };
  if (
    metrics.sttMs === null
    || metrics.llmTtftMs === null
    || metrics.ttsTtfbMs === null
    || metrics.e2eMs === null
  ) {
    return { className: "idle", label: "数据不足", value: null };
  }
  return metrics.e2eMs < 1200
    ? { className: "fast", label: "首包极速", value: metrics.e2eMs }
    : metrics.e2eMs < 2500
      ? { className: "good", label: "首包良好", value: metrics.e2eMs }
      : { className: "slow", label: "首包偏高", value: metrics.e2eMs };
}
