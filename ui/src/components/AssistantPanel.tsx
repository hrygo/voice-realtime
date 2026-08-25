import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useEventSocket } from "../hooks/useEventSocket";
import type { CommandSocketApi } from "../hooks/useCommandSocket";
import type { AssistantPhase, AssistantBubble, TurnMetrics } from "../stores/assistantStore";
import {
  parseAssistantEvent,
  selectAssistantConnected,
  selectAssistantLatestMetrics,
  selectAssistantPhase,
  selectAssistantTranscript,
  selectLastInterruptionTime,
  useAssistantStore,
} from "../stores/assistantStore";
import {
  BUILTIN_PERSONAS,
  useUISettingsStore,
  type DuplexMode,
  type PersonaTemplate,
} from "../stores/uiSettingsStore";
import { MarkdownRenderer } from "./meeting/MarkdownRenderer";
import { showToast } from "./Toast";
import {
  ActivityIcon,
  BroomIcon,
  DownloadIcon,
  EditIcon,
  FileTextIcon,
  HeadphonesIcon,
  MaskIcon,
  RefreshCwIcon,
  SoundWaveAnimatedIcon,
  SpeakerIcon,
  StopCircleIcon,
  TrashIcon,
  WrenchIcon,
} from "./Icons";
import "./AssistantPanel.css";

type Command = "clear_context" | "stop_session" | "restart";
const PHASE_CONFIG: Record<
  AssistantPhase,
  { label: string; icon: string; desc: string; className: string }
> = {
  idle: { label: "待命", icon: "💤", desc: "系统就绪，请直接说话", className: "phase-idle" },
  listening: { label: "聆听", icon: "👂", desc: "正在接收麦克风语音...", className: "phase-listening" },
  thinking: { label: "思考", icon: "🧠", desc: "LM Studio 推理生成中...", className: "phase-thinking" },
  speaking: { label: "播报", icon: "🗣️", desc: "Qwen3-TTS 语音播报中...", className: "phase-speaking" },
  degraded: { label: "降级", icon: "⚠️", desc: "交互链路异常，请检查服务状态", className: "phase-idle" },
  stopped: { label: "已停止", icon: "⏹️", desc: "语音交互会话已停止", className: "phase-idle" },
};

const FALLBACK_VOICES: readonly string[] = ["default", "warm", "bright", "calm"];

/**
 * 给“用户开始说话”留出一个可感知的视觉窗口。
 * 实际管道状态仍按事件即时更新，这个延迟只作用于助手面板的展示层。
 */
export const LISTENING_TO_THINKING_MIN_VISIBLE_MS = 240;

export function getAssistantPhaseTransitionDelay(
  displayedPhase: AssistantPhase,
  nextPhase: AssistantPhase,
  phaseStartedAt: number,
  now = Date.now(),
): number {
  if (displayedPhase !== "listening" || nextPhase !== "thinking") return 0;

  const elapsed = Math.max(0, now - phaseStartedAt);
  return Math.max(0, LISTENING_TO_THINKING_MIN_VISIBLE_MS - elapsed);
}

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

const DUPLEX_MODES: readonly DuplexMode[] = ["speaker_focus", "headphone_duplex"];

const VOICE_CONFIGS: Record<string, { label: string; tag: string }> = {
  default: { label: "默认原声", tag: "标准" },
  warm: { label: "温暖磁性", tag: "亲和" },
  bright: { label: "清脆干练", tag: "活力" },
  calm: { label: "沉稳专业", tag: "严谨" },
};

function formatMetric(value: number | null): string {
  return value === null ? "—" : `${value}ms`;
}

export interface TelemetryBadge {
  readonly className: "fast" | "good" | "slow" | "idle";
  readonly label: "极速" | "良好" | "偏高" | "数据不足" | "待命中";
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
    title: "STT 识别",
    formula: "max(0, STT final − 说话结束)",
    event: "UserStoppedSpeakingFrame → TranscriptionFrame",
    description: "用户停止说话后，到语音转写产出最终文本的等待时间。若 final 已先于静音帧到达，则按 0ms 计，不把说话时长算进去。",
  },
  {
    title: "LLM 首字",
    formula: "LLM 首字 − max(STT final, 说话结束)",
    event: "TranscriptionFrame / UserStoppedSpeakingFrame → LLMTextFrame",
    description: "转写完成且用户回合结束后，到大模型输出第一段文本的等待时间；不代表完整回答生成完成。",
  },
  {
    title: "TTS 首包",
    formula: "TTS 首帧 − LLM 首字",
    event: "LLMTextFrame → TTSAudioRawFrame",
    description: "大模型开始输出文本后，到语音合成送出第一帧音频的等待时间；不代表整段语音已经播放完。",
  },
] as const;

type TelemetryMetrics = Pick<TurnMetrics, "sttMs" | "llmTtftMs" | "ttsTtfbMs" | "e2eMs">;

export function getTelemetryBadge(metrics: TelemetryMetrics | null): TelemetryBadge {
  if (metrics === null) {
    return { className: "idle", label: "待命中", value: null };
  }

  if (
    metrics.sttMs === null
    || metrics.llmTtftMs === null
    || metrics.ttsTtfbMs === null
    || metrics.e2eMs === null
  ) {
    return { className: "idle", label: "数据不足", value: null };
  }

  return metrics.e2eMs < 1200
    ? { className: "fast", label: "极速", value: metrics.e2eMs }
    : metrics.e2eMs < 2500
      ? { className: "good", label: "良好", value: metrics.e2eMs }
      : { className: "slow", label: "偏高", value: metrics.e2eMs };
}

interface AssistantPanelProps {
  readonly commandSocket: CommandSocketApi;
  readonly isMeetingRecording?: boolean;
  readonly onNavigateMeeting?: () => void;
}

export default function AssistantPanel({
  commandSocket,
  isMeetingRecording = false,
  onNavigateMeeting,
}: AssistantPanelProps) {
  const phase = useAssistantStore(selectAssistantPhase);
  const visiblePhase = useDisplayedAssistantPhase(phase);
  const transcript = useAssistantStore(selectAssistantTranscript);
  const connected = useAssistantStore(selectAssistantConnected);
  const lastInterruptionTime = useAssistantStore(selectLastInterruptionTime);
  const latestMetrics = useAssistantStore(selectAssistantLatestMetrics);
  const telemetryBadge = getTelemetryBadge(latestMetrics);
  const clearTranscript = useAssistantStore((state) => state.clearTranscript);

  const transcriptScrollRef = useRef<HTMLDivElement>(null);
  const telemetryHelpRef = useRef<HTMLDivElement>(null);
  const [isScrolledUp, setIsScrolledUp] = useState(false);
  const [interruptionActive, setInterruptionActive] = useState(false);
  const [telemetryHelpOpen, setTelemetryHelpOpen] = useState(false);
  const [textInput, setTextInput] = useState("");
  const [pendingDuplexMode, setPendingDuplexMode] = useState<DuplexMode | null>(null);
  const [duplexSwitchError, setDuplexSwitchError] = useState<string | null>(null);

  /* ---- 声音输出与插话模式 ---- */
  const duplexMode = useUISettingsStore((s) => s.duplexMode);
  const duplexPresentation = DUPLEX_MODE_PRESENTATION[duplexMode];

  /* ---- 人格与人设管理 ---- */
  const [personaOpen, setPersonaOpen] = useState(false);
  const persona = useUISettingsStore((s) => s.persona);
  const customPersonas = useUISettingsStore((s) => s.customPersonas);
  const addCustomPersona = useUISettingsStore((s) => s.addCustomPersona);
  const removeCustomPersona = useUISettingsStore((s) => s.removeCustomPersona);

  const [personaDraft, setPersonaDraft] = useState(persona);
  const [newTemplateName, setNewTemplateName] = useState("");
  const [showAddCustom, setShowAddCustom] = useState(false);
  const [personaError, setPersonaError] = useState("");

  /* ---- 音色选择 ---- */
  const voice = useUISettingsStore((s) => s.voice);
  const micMuted = useUISettingsStore((s) => s.micMuted);
  const [availableVoices, setAvailableVoices] = useState<readonly string[]>(FALLBACK_VOICES);
  const [isPreviewPlaying, setIsPreviewPlaying] = useState(false);

  // 打断插话动效监听
  useEffect(() => {
    if (!lastInterruptionTime || !duplexPresentation.interruptionEnabled) {
      setInterruptionActive(false);
      return;
    }
    setInterruptionActive(true);
    const timer = setTimeout(() => setInterruptionActive(false), 3500);
    return () => clearTimeout(timer);
  }, [duplexPresentation.interruptionEnabled, lastInterruptionTime]);

  // 遥测说明是轻量 popover：点击外部区域或按 Esc 即可关闭。
  useEffect(() => {
    if (!telemetryHelpOpen) return;

    const handlePointerDown = (event: PointerEvent) => {
      if (telemetryHelpRef.current?.contains(event.target as Node)) return;
      setTelemetryHelpOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setTelemetryHelpOpen(false);
    };

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [telemetryHelpOpen]);

  /** 发送通用命令 */
  const sendCommandWith = useCallback(
    async (payload: Parameters<CommandSocketApi["sendCommand"]>[0], toastMsg?: string) => {
      if (!commandSocket.ready) {
        showToast("控制端连接中，请稍候...", "warning");
        return false;
      }
      try {
        await commandSocket.sendCommand(payload);
        if (toastMsg) showToast(toastMsg, "success");
        return true;
      } catch (error) {
        showToast(error instanceof Error ? error.message : "指令发送失败", "error");
        return false;
      }
    },
    [commandSocket],
  );

  const handleDuplexModeChange = useCallback(
    async (mode: DuplexMode) => {
      if (!canRequestDuplexModeChange(duplexMode, mode, pendingDuplexMode)) return;
      if (!commandSocket.ready) {
        showToast("控制端连接中，请稍候...", "warning");
        return;
      }

      const targetLabel = DUPLEX_MODE_PRESENTATION[mode].label;
      setPendingDuplexMode(mode);
      setDuplexSwitchError(null);

      const acknowledged = await sendCommandWith(
        { cmd: "set_duplex_mode", mode },
      );
      if (!acknowledged) {
        setDuplexSwitchError(`切换到「${targetLabel}」未完成`);
      }
      setPendingDuplexMode(null);
    },
    [commandSocket.ready, duplexMode, pendingDuplexMode, sendCommandWith],
  );

  const sendCommand = useCallback(
    (command: Command) => {
      const msgMap: Record<Command, string> = {
        clear_context: "已清空 LLM 上下文记忆",
        stop_session: "已停止语音交互会话",
        restart: "已下发管道重启指令",
      };
      void sendCommandWith({ cmd: command }, msgMap[command]);
    },
    [sendCommandWith],
  );

  /** 打开人格编辑器 */
  const openPersona = useCallback(() => {
    setPersonaDraft(persona);
    setPersonaError("");
    setShowAddCustom(false);
    setPersonaOpen(true);
  }, [persona]);

  /** 保存人格 */
  const savePersona = useCallback(async () => {
    const trimmed = personaDraft.trim();
    if (!trimmed) {
      setPersonaError("系统提示词不能为空");
      return;
    }
    const acknowledged = await sendCommandWith(
      { cmd: "set_persona", prompt: trimmed },
      "人格提示词已更新并生效",
    );
    if (acknowledged) {
      setPersonaOpen(false);
    }
  }, [personaDraft, sendCommandWith]);

  /** 取消编辑 */
  const cancelPersona = useCallback(() => {
    setPersonaOpen(false);
    setPersonaError("");
  }, []);

  /** 保存为自定义人设 */
  const handleSaveCustom = useCallback(() => {
    if (!newTemplateName.trim()) {
      showToast("请输入人设名称", "warning");
      return;
    }
    if (!personaDraft.trim()) {
      showToast("提示词不能为空", "warning");
      return;
    }
    addCustomPersona(newTemplateName, personaDraft);
    setNewTemplateName("");
    setShowAddCustom(false);
    showToast("已保存至自定义人设库", "success");
  }, [newTemplateName, personaDraft, addCustomPersona]);

  /** 音色切换 */
  const handleVoiceChange = useCallback(
    async (value: string) => {
      const acknowledged = await sendCommandWith({ cmd: "set_voice", voice: value });
      if (acknowledged) {
        showToast(`音色已切换为: ${value}`, "success");
      }
    },
    [sendCommandWith],
  );

  /** 试听音色：请求后端 /v1/audio/speech 获取真实音频并播放 */
  const handlePreviewVoice = useCallback(
    async (v: string) => {
      if (isPreviewPlaying) return;
      setIsPreviewPlaying(true);
      showToast(`🔊 正在生成音色 [${v}] 试听...`, "info");

      let blob: Blob;
      try {
        const previewText = "你好，我是你的语音助手，很高兴为你服务。";
        const res = await fetch("/v1/audio/speech", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            model: "VoiceDesign",
            input: previewText,
            voice: v,
            response_format: "wav",
          }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        blob = await res.blob();
      } catch (err) {
        setIsPreviewPlaying(false);
        showToast("试听请求失败，请确保 TTS 桥已启动", "error");
        return;
      }

      try {
        const audioUrl = URL.createObjectURL(blob);
        const audio = new Audio(audioUrl);
        audio.onended = () => {
          setIsPreviewPlaying(false);
          URL.revokeObjectURL(audioUrl);
        };
        audio.onerror = () => {
          setIsPreviewPlaying(false);
          URL.revokeObjectURL(audioUrl);
          showToast("试听音频解码或播放失败", "error");
        };
        await audio.play();
      } catch (playErr) {
        setIsPreviewPlaying(false);
        showToast("音频播放被浏览器拦截，请点击页面后重试", "error");
      }
    },
    [isPreviewPlaying],
  );

  /** 文字兜底发送 */
  const handleSendText = async () => {
    const trimmed = textInput.trim();
    if (!trimmed) return;
    if (!commandSocket.ready) {
      showToast("控制端连接中，请稍候...", "warning");
      return;
    }
    setTextInput("");
    const success = await sendCommandWith(
      { cmd: "send_text", text: trimmed },
      "已发送输入文本",
    );
    if (!success) {
      setTextInput(trimmed);
    }
  };

  /** 导出对话 */
  const handleExportChat = (format: "md" | "txt") => {
    if (!transcript.length) {
      showToast("暂无对话记录可导出", "warning");
      return;
    }
    let content = "";
    if (format === "md") {
      content = `# Voice Studio 语音交互记录\n\n- 日期: ${new Date().toLocaleString()}\n- 提示词人格: ${persona}\n- 音色: ${voice}\n\n---\n\n` +
        transcript.map((b) => `### ${b.role === "user" ? "👤 用户" : "🤖 助手"} (${b.timestamp || ""})\n\n${b.text}\n`).join("\n");
    } else {
      content = transcript.map((b) => `[${b.role === "user" ? "用户" : "助手"}] ${b.text}`).join("\n\n");
    }
    const blob = new Blob([content], { type: format === "md" ? "text/markdown" : "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `voice-assistant-chat-${new Date().toISOString().substring(0, 10)}.${format}`;
    a.click();
    URL.revokeObjectURL(url);
    showToast(`已成功导出对话记录 (.${format})`, "success");
  };

  /** 结束当前会议录制 */
  const handleStopMeeting = useCallback(async () => {
    const acknowledged = await sendCommandWith(
      { cmd: "stop_active_mode" },
      "已结束会议录制，系统恢复待命",
    );
    if (acknowledged) {
      showToast("会议录制已结束", "info");
    }
  }, [sendCommandWith]);

  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [playingBubbleKey, setPlayingBubbleKey] = useState<string | null>(null);

  /** 重新朗读气泡文本 */
  const handleReplayBubbleVoice = useCallback(
    async (text: string, key: string) => {
      if (playingBubbleKey) return;
      setPlayingBubbleKey(key);
      showToast("🔊 正在合成语音并朗读...", "info");

      try {
        const res = await fetch("/v1/audio/speech", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            model: "VoiceDesign",
            input: text.slice(0, 500),
            voice,
            response_format: "wav",
          }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        const audioUrl = URL.createObjectURL(blob);
        const audio = new Audio(audioUrl);
        audio.onended = () => {
          setPlayingBubbleKey(null);
          URL.revokeObjectURL(audioUrl);
        };
        audio.onerror = () => {
          setPlayingBubbleKey(null);
          URL.revokeObjectURL(audioUrl);
          showToast("语音播放失败", "error");
        };
        await audio.play();
      } catch {
        setPlayingBubbleKey(null);
        showToast("语音朗读请求失败，请确保 TTS 桥已启动", "error");
      }
    },
    [playingBubbleKey, voice],
  );

  /** 复制气泡文本 */
  const handleCopyBubble = useCallback((text: string, key: string) => {
    navigator.clipboard.writeText(text).then(
      () => {
        setCopiedKey(key);
        showToast("已复制对话内容", "success");
        setTimeout(() => setCopiedKey((prev) => (prev === key ? null : prev)), 2000);
      },
      () => showToast("复制失败", "error"),
    );
  }, []);

  /** WebSocket 消息解析 */
  const handleMessage = useCallback((message: MessageEvent) => {
    if (typeof message.data !== "string") return;
    try {
      const event = parseAssistantEvent(JSON.parse(message.data));
      if (event) useAssistantStore.getState().applyEvent(event);
    } catch {
      // Ignore
    }
  }, []);

  const { state: socketState } = useEventSocket("/ws/assistant", handleMessage);

  useEffect(() => {
    useAssistantStore.getState().setConnected(socketState === "open");
  }, [socketState]);

  /** 智能自动滚动处理 */
  const handleScroll = useCallback(() => {
    const el = transcriptScrollRef.current;
    if (!el) return;
    const distanceToBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setIsScrolledUp(distanceToBottom > 80);
  }, []);

  const scrollToBottom = useCallback(() => {
    const el = transcriptScrollRef.current;
    if (el) {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
      setIsScrolledUp(false);
    }
  }, []);

  useEffect(() => {
    if (document.hidden || isScrolledUp) return;
    const frame = requestAnimationFrame(() => {
      const el = transcriptScrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [transcript, isScrolledUp]);

  /** 获取音色列表 */
  useEffect(() => {
    let cancelled = false;
    fetch("/v1/voices")
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<{ voice: string; available: string[] }>;
      })
      .then((data) => {
        if (cancelled) return;
        if (data.available && data.available.length > 0) {
          setAvailableVoices(data.available);
        }
      })
      .catch(() => {
        // Fallback silently
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /** 快捷键监听 */
  useEffect(() => {
    const handleGlobalShortcuts = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPersonaOpen((prev) => !prev);
      } else if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "c") {
        e.preventDefault();
        sendCommand("clear_context");
      }
    };
    window.addEventListener("keydown", handleGlobalShortcuts);
    return () => window.removeEventListener("keydown", handleGlobalShortcuts);
  }, [sendCommand]);

  const handleSelectPersona = useCallback(
    async (prompt: string, name: string) => {
      setPersonaDraft(prompt);
      const acknowledged = await sendCommandWith(
        { cmd: "set_persona", prompt },
        `已切换为人设：“${name}”`,
      );
      if (!acknowledged) {
        showToast("切换人设失败", "error");
      }
    },
    [sendCommandWith],
  );

  const currentPhaseConfig = PHASE_CONFIG[visiblePhase];
  const allTemplates: readonly PersonaTemplate[] = [...BUILTIN_PERSONAS, ...customPersonas];
  const isCustomActive = !allTemplates.some((preset) => persona.trim() === preset.prompt.trim());
  const duplexToggleMode = getDuplexToggleMode(duplexMode, pendingDuplexMode);
  const duplexFeedback = getDuplexModeFeedback(
    duplexMode,
    pendingDuplexMode,
    commandSocket.ready,
    duplexSwitchError ?? undefined,
  );

  return (
    <div className="assistant-workspace">
      {/* 左侧控制与人设侧边栏 (标准化模块化设计) */}
      <aside className="assistant-sidebar">
        {/* Group 1: 声音输出与插话 */}
        <div className="sidebar-group">
          <div className="sidebar-group-header">
            <span className="sidebar-group-title">
              <span className="group-title-badge">
                <HeadphonesIcon size={13} />
              </span>
              声音输出与插话
            </span>
            <span className={`duplex-sync-badge tone-${duplexFeedback.tone}`}>
              {duplexFeedback.tone === "switching"
                ? "应用中"
                : duplexFeedback.tone === "active"
                  ? "已生效"
                  : duplexFeedback.tone === "offline"
                    ? "连接中"
                    : "未完成"}
            </span>
          </div>

          <div
            className={`duplex-toggle mode-${duplexToggleMode} ${pendingDuplexMode !== null ? "is-switching" : ""}`}
            role="radiogroup"
            aria-label="声音输出与插话模式"
            aria-busy={pendingDuplexMode !== null}
          >
            <span className="duplex-toggle-track" aria-hidden="true">
              <span className="duplex-toggle-thumb">
                {pendingDuplexMode !== null && <span className="duplex-toggle-spinner" />}
              </span>
            </span>
            {DUPLEX_MODES.map((mode) => {
              const presentation = DUPLEX_MODE_PRESENTATION[mode];
              const committed = duplexMode === mode;
              const selected = duplexToggleMode === mode;
              const switching = pendingDuplexMode === mode;
              return (
                <button
                  key={mode}
                  type="button"
                  className={`duplex-toggle-option ${selected ? "selected" : ""} ${switching ? "switching" : ""}`}
                  onClick={() => void handleDuplexModeChange(mode)}
                  disabled={!commandSocket.ready || pendingDuplexMode !== null}
                  role="radio"
                  aria-checked={committed}
                  aria-busy={switching}
                  aria-label={`${presentation.label}：${presentation.summary}`}
                  title={presentation.detail}
                >
                  {mode === "speaker_focus" ? (
                    <SpeakerIcon size={15} className="duplex-toggle-icon" />
                  ) : (
                    <HeadphonesIcon size={15} className="duplex-toggle-icon" />
                  )}
                  <span className="duplex-toggle-label">{presentation.label}</span>
                </button>
              );
            })}
          </div>

          <div
            className={`duplex-mode-feedback tone-${duplexFeedback.tone}`}
            role={duplexFeedback.tone === "error" ? "alert" : "status"}
            aria-live="polite"
          >
            <span className="duplex-feedback-indicator" aria-hidden="true" />
            <span className="duplex-feedback-copy">
              <strong>{duplexFeedback.title}</strong>
              <span>{duplexFeedback.detail}</span>
            </span>
          </div>
        </div>

        {/* Group 2: 角色与声音配置 */}
        <div className="sidebar-group">
          <div className="sidebar-group-header">
            <span className="sidebar-group-title">
              <span className="group-title-badge">
                <MaskIcon size={13} />
              </span>
              角色与声音
            </span>
          </div>

          <div className="sidebar-field-block">
            <div className="sidebar-field-label-row">
              <label htmlFor="assistant-persona-select" className="sidebar-field-label">
                助手人设
              </label>
              <button
                type="button"
                className="sidebar-link-btn"
                onClick={openPersona}
                title="管理与定制人设提示词 (快捷键 Cmd+K)"
              >
                <EditIcon size={11} />
                <span>管理</span>
                <kbd className="sidebar-link-kbd">⌘K</kbd>
              </button>
            </div>
            <div className="sidebar-select-wrap">
              <select
                id="assistant-persona-select"
                className="sidebar-select"
                value={isCustomActive ? "__custom__" : persona}
                onChange={(e) => {
                  if (e.target.value === "__custom__") {
                    openPersona();
                    return;
                  }
                  const selected = allTemplates.find((t) => t.prompt === e.target.value);
                  if (selected) {
                    void handleSelectPersona(selected.prompt, selected.name);
                  }
                }}
              >
                {allTemplates.map((preset) => (
                  <option key={preset.id} value={preset.prompt}>
                    {preset.name} {!preset.isBuiltin ? "(自定义)" : ""}
                  </option>
                ))}
                {isCustomActive && <option value="__custom__">当前自定义人设 (编辑中)</option>}
              </select>
            </div>
            <div className="persona-preview-card" onClick={openPersona} title="点击展开编辑系统提示词">
              <p className="persona-preview-text">{persona || "你是一个聪明的全本地语音助手。"}</p>
              <span className="persona-preview-hint">编辑 ↗</span>
            </div>
          </div>

          <div className="sidebar-field-block">
            <div className="sidebar-field-label-row">
              <label htmlFor="assistant-voice-select" className="sidebar-field-label">
                播报音色
              </label>
              {VOICE_CONFIGS[voice] && (
                <span className="sidebar-field-badge">
                  {VOICE_CONFIGS[voice].tag}
                </span>
              )}
            </div>
            <div className="voice-input-group">
              <div className="sidebar-select-wrap voice-select-wrap">
                <select
                  id="assistant-voice-select"
                  className="sidebar-select voice-select"
                  value={voice}
                  onChange={(e) => void handleVoiceChange(e.target.value)}
                  disabled={!commandSocket.ready}
                >
                  {availableVoices.map((v) => {
                    const desc = VOICE_CONFIGS[v];
                    return (
                      <option key={v} value={v}>
                        {desc ? `${desc.label} (${v})` : v}
                      </option>
                    );
                  })}
                </select>
              </div>
              <button
                type="button"
                className={`btn-voice-audition ${isPreviewPlaying ? "playing" : ""}`}
                onClick={() => void handlePreviewVoice(voice)}
                disabled={isPreviewPlaying}
                title="生成并播放当前音色试听"
              >
                <SoundWaveAnimatedIcon size={13} isPlaying={isPreviewPlaying} />
                <span>{isPreviewPlaying ? "试听中" : "试听"}</span>
              </button>
            </div>
          </div>
        </div>

        {/* Group 3: 交互时延遥测 */}
        <div className="sidebar-group telemetry-group" ref={telemetryHelpRef}>
          <div className="sidebar-group-header">
            <div className="telemetry-title-wrap">
              <span className="sidebar-group-title">
                <span className="group-title-badge">
                  <ActivityIcon size={13} />
                </span>
                交互时延遥测
              </span>
              <button
                type="button"
                className={`telemetry-help-trigger ${telemetryHelpOpen ? "open" : ""}`}
                aria-expanded={telemetryHelpOpen}
                aria-controls="telemetry-help-panel"
                aria-label="查看交互时延算法说明"
                title="查看交互时延算法说明"
                onClick={() => setTelemetryHelpOpen((open) => !open)}
              >
                ?
              </button>
            </div>
            <span className={`telemetry-grade-pill ${telemetryBadge.className}`}>
              {telemetryBadge.value === null
                ? telemetryBadge.label
                : `${telemetryBadge.label} · ${formatMetric(telemetryBadge.value)}`}
            </span>
          </div>

          {telemetryHelpOpen && (
            <div
              id="telemetry-help-panel"
              className="telemetry-help-panel"
              role="region"
              aria-labelledby="telemetry-help-title"
            >
              <div className="telemetry-help-header">
                <div>
                  <span className="telemetry-help-kicker">计时口径</span>
                  <strong id="telemetry-help-title">从用户说话结束开始</strong>
                </div>
                <button
                  type="button"
                  className="telemetry-help-close"
                  aria-label="关闭交互时延算法说明"
                  onClick={() => setTelemetryHelpOpen(false)}
                >
                  ×
                </button>
              </div>
              <p className="telemetry-help-intro">
                后端用单调时钟记录帧到达时间，所有数值显示到 1 位小数；只统计首个可观测事件，不估算未到达的阶段。
              </p>
              <div className="telemetry-help-steps">
                {TELEMETRY_HELP_STEPS.map((step, index) => (
                  <div className="telemetry-help-step" key={step.title}>
                    <div className="telemetry-help-step-header">
                      <span className="telemetry-help-step-index">{String(index + 1).padStart(2, "0")}</span>
                      <div className="telemetry-help-step-copy">
                        <strong>{step.title}</strong>
                        <code>{step.formula}</code>
                      </div>
                    </div>
                    <p>{step.description}</p>
                    <span className="telemetry-help-event">帧：{step.event}</span>
                  </div>
                ))}
              </div>
              <div className="telemetry-help-total">
                <div>
                  <strong>端到端（E2E）</strong>
                  <code>STT + LLM + TTS</code>
                </div>
                <span>三段数据齐全时按已显示值相加；缺少任一关键帧则显示“数据不足”。</span>
              </div>
              <p className="telemetry-help-note">
                不包含用户说话持续时间、LLM 完整生成时间或 TTS 剩余播放时间。
              </p>
            </div>
          )}

          {latestMetrics ? (
            <div className="telemetry-compact-flow">
              <div className="flow-step">
                <span className="step-name">STT 识别</span>
                <span className="step-time">{formatMetric(latestMetrics.sttMs)}</span>
              </div>
              <span className="flow-sep">→</span>
              <div className="flow-step">
                <span className="step-name">LLM 首字</span>
                <span className="step-time">{formatMetric(latestMetrics.llmTtftMs)}</span>
              </div>
              <span className="flow-sep">→</span>
              <div className="flow-step">
                <span className="step-name">TTS 首包</span>
                <span className="step-time">{formatMetric(latestMetrics.ttsTtfbMs)}</span>
              </div>
            </div>
          ) : (
            <div className="telemetry-idle-notice">
              <span className="idle-dot" />
              <span>对话后实时呈现全链路耗时遥测</span>
            </div>
          )}
        </div>

        {/* Group 4: 会话与记录操作 */}
        <div className="sidebar-group">
          <div className="sidebar-group-header">
            <span className="sidebar-group-title">
              <span className="group-title-badge">
                <WrenchIcon size={13} />
              </span>
              会话与控制
            </span>
          </div>

          <div className="session-action-grid">
            <button
              type="button"
              className="btn-action-ghost"
              onClick={() => sendCommand("clear_context")}
              disabled={!commandSocket.ready}
              title="清空 LLM 上下文记忆 (快捷键 Cmd+Shift+C)"
            >
              <BroomIcon size={13} className="btn-action-icon" />
              <span>清空记忆</span>
            </button>
            <button
              type="button"
              className="btn-action-ghost"
              onClick={() => {
                clearTranscript();
                showToast("对话记录已清空", "info");
              }}
              disabled={!transcript.length}
              title="清空屏幕对话记录"
            >
              <TrashIcon size={13} className="btn-action-icon" />
              <span>清空屏幕</span>
            </button>
            <button
              type="button"
              className="btn-action-ghost"
              onClick={() => sendCommand("restart")}
              disabled={!commandSocket.ready}
              title="重启后端交互管道"
            >
              <RefreshCwIcon size={13} className="btn-action-icon" />
              <span>重启管道</span>
            </button>
            <button
              type="button"
              className="btn-action-ghost danger"
              onClick={() => sendCommand("stop_session")}
              disabled={!commandSocket.ready}
              title="停止语音会话"
            >
              <StopCircleIcon size={13} className="btn-action-icon" />
              <span>停止会话</span>
            </button>
          </div>

          <div className="sidebar-export-row">
            <span className="export-label">导出记录</span>
            <div className="export-chips">
              <button
                type="button"
                className="btn-chip"
                onClick={() => handleExportChat("md")}
                disabled={!transcript.length}
                title="导出为 Markdown 格式"
              >
                <FileTextIcon size={11} />
                <span>Markdown</span>
              </button>
              <button
                type="button"
                className="btn-chip"
                onClick={() => handleExportChat("txt")}
                disabled={!transcript.length}
                title="导出为纯文本格式"
              >
                <DownloadIcon size={11} />
                <span>纯文本</span>
              </button>
            </div>
          </div>
        </div>
      </aside>

      {/* 右侧主互动区 */}
      <main className="assistant-main-stage">
        {/* 头部 */}
        <header className="assistant-stage-header">
          <div className="assistant-header-title-wrap">
            <h2>
              <span>🤖</span> 实时语音互动
            </h2>
            <span className={`assistant-connection-badge ${connected ? "connected" : ""}`}>
              <span className="assistant-connection-dot" />
              {connected ? "状态桥已连接" : "连接中..."}
            </span>
          </div>

          <div
            className={`assistant-phase-badge ${currentPhaseConfig.className}`}
            title={currentPhaseConfig.desc}
          >
            <span>{currentPhaseConfig.icon}</span>
            <span>{currentPhaseConfig.label}</span>
          </div>
        </header>

        {isMeetingRecording && (
          <div className="assistant-meeting-suspension-banner" role="alert">
            <div className="suspension-banner-text">
              <span className="suspension-icon">🎙️</span>
              <div className="suspension-desc-wrap">
                <strong>会议录制进行中 · 语音交互已自动挂起</strong>
                <p>
                  为彻底防止扬声器 TTS 播报回声污染会议纪要与声纹分轨，语音交互已安全挂起。
                </p>
              </div>
            </div>
            <div className="suspension-banner-actions">
              {onNavigateMeeting && (
                <button
                  type="button"
                  className="suspension-action-btn primary"
                  onClick={onNavigateMeeting}
                >
                  查看进行中会议 →
                </button>
              )}
              <button
                type="button"
                className="suspension-action-btn danger"
                onClick={() => void handleStopMeeting()}
                disabled={!commandSocket.ready}
                title="结束正在录制的会议并恢复待命"
              >
                ⏹ 结束会议录制
              </button>
            </div>
          </div>
        )}

        {/* 状态步骤指示栏 + 打断插话指示 */}
        <div className="assistant-phase-bar" role="status" aria-label="助手处理阶段">
          <div className={`phase-step-item ${visiblePhase === "listening" ? "active step-listening" : ""}`}>
            <span className="phase-step-icon">👂</span>
            <span>聆听麦克风</span>
          </div>
          <div className={`phase-step-item ${visiblePhase === "thinking" ? "active step-thinking" : ""}`}>
            <span className="phase-step-icon">🧠</span>
            <span>LM Studio 推理</span>
          </div>
          <div className={`phase-step-item ${visiblePhase === "speaking" ? "active step-speaking" : ""}`}>
            <span className="phase-step-icon">🗣️</span>
            <span>Qwen3-TTS 播报</span>
          </div>

          {interruptionActive && duplexPresentation.interruptionEnabled && (
            <div className="interruption-alert-chip" role="alert">
              <span>⚡ 已响应插话打断 (耳机)</span>
            </div>
          )}
        </div>

        {/* 60FPS 声学动态可视化波形 */}
        <div className="assistant-waveform-container">
          <AssistantWaveform phase={visiblePhase} isMuted={micMuted} />
        </div>

        {/* 对话气泡流 */}
        <div className="assistant-transcript-container">
          <div
            className="assistant-transcript"
            ref={transcriptScrollRef}
            onScroll={handleScroll}
            aria-live="polite"
          >
            {transcript.map((bubble: AssistantBubble, idx: number) => {
              const bubbleKey = `${bubble.role}-${bubble.turnId ?? idx}-${bubble.timestamp ?? idx}`;
              const isCopied = copiedKey === bubbleKey;
              return (
                <div
                  className={`assistant-bubble-row ${bubble.role}`}
                  key={bubbleKey}
                >
                  <div className="bubble-meta-header">
                    <span className="bubble-role-badge">
                      {bubble.role === "user" ? "👤 你" : "🤖 AI 助手"}
                    </span>
                    {bubble.turnId !== undefined && (
                      <span className="bubble-turn-pill">#{bubble.turnId}</span>
                    )}
                    {bubble.timestamp && (
                      <span className="bubble-time-pill">{bubble.timestamp}</span>
                    )}
                    {bubble.interrupted && (
                      <span className="bubble-interrupted-tag">⚡ 已打断 (耳机插话)</span>
                    )}
                  </div>
                  <div className={`bubble-card ${bubble.final ? "final" : "streaming"}`}>
                    {bubble.role === "assistant" ? (
                      <MarkdownRenderer content={bubble.text} />
                    ) : (
                      <span>{bubble.text}</span>
                    )}
                    <div className="bubble-actions-group">
                      {bubble.role === "assistant" && bubble.final && (
                        <button
                          type="button"
                          className={`bubble-action-btn ${playingBubbleKey === bubbleKey ? "playing" : ""}`}
                          onClick={() => void handleReplayBubbleVoice(bubble.text, bubbleKey)}
                          disabled={playingBubbleKey !== null}
                          title="使用当前音色重新朗读此条回复"
                        >
                          {playingBubbleKey === bubbleKey ? "🔊 播报中..." : "🔊 朗读"}
                        </button>
                      )}
                      <button
                        type="button"
                        className={`bubble-action-btn ${isCopied ? "copied" : ""}`}
                        onClick={() => handleCopyBubble(bubble.text, bubbleKey)}
                        title="复制内容"
                      >
                        {isCopied ? "✓ 已复制" : "📋 复制"}
                      </button>
                    </div>
                  </div>
                </div>
              );
            })}

            {!transcript.length && (
              <div className="assistant-empty-state">
                <span className="empty-state-icon">🎙️</span>
                <p className="empty-state-title">等待语音输入...</p>
                <p className="empty-state-desc">
                  直接对着麦克风说话，AI 助手将实时转写、推理并语音应答。
                  {duplexPresentation.summary}。
                </p>
              </div>
            )}
          </div>

          {/* 智能贴底按钮 */}
          {isScrolledUp && (
            <button
              type="button"
              className="scroll-to-bottom-btn"
              onClick={scrollToBottom}
              aria-label="回到底部"
            >
              <span>↓</span> 最新对话
            </button>
          )}

          {/* 文字输入兜底栏 (Text-to-Chat) */}
          <div className="assistant-input-bar">
            <input
              type="text"
              className="assistant-text-input"
              placeholder="💬 输入文字与助手对话 (按 Enter 发送)..."
              value={textInput}
              onChange={(e) => setTextInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  handleSendText();
                }
              }}
            />
            <button
              type="button"
              className="assistant-send-btn"
              onClick={handleSendText}
              disabled={!textInput.trim()}
              title="发送文字消息"
            >
              <span>↑</span> 发送
            </button>
          </div>
        </div>
      </main>

      {/* 人格与提示词管理器 Modal */}
      {personaOpen && (
        <div
          className="persona-modal-overlay"
          role="dialog"
          aria-modal="true"
          aria-labelledby="persona-modal-title"
          onClick={cancelPersona}
        >
          <div className="persona-modal-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="persona-dialog-header">
              <h3 id="persona-modal-title">
                <span>🎭</span> 助手人设库与提示词定制
              </h3>
              <button
                type="button"
                className="persona-dialog-close"
                onClick={cancelPersona}
                aria-label="关闭"
              >
                ✕
              </button>
            </div>

            <div className="persona-dialog-body">
              <div className="persona-presets-section">
                <div className="presets-header-line">
                  <span className="presets-label">人设模板库 (点击载入)</span>
                  <button
                    type="button"
                    className="preset-chip"
                    onClick={() => setShowAddCustom(!showAddCustom)}
                  >
                    {showAddCustom ? "取消新增" : "+ 存为新模板"}
                  </button>
                </div>

                <div className="presets-chips">
                  {allTemplates.map((preset) => {
                    const isSelected = personaDraft.trim() === preset.prompt.trim();
                    return (
                      <span
                        key={preset.id}
                        className={`preset-chip ${isSelected ? "active" : ""}`}
                        onClick={() => setPersonaDraft(preset.prompt)}
                      >
                        {preset.name}
                        {!preset.isBuiltin && (
                          <span
                            className="preset-delete-icon"
                            onClick={(e) => {
                              e.stopPropagation();
                              removeCustomPersona(preset.id);
                              showToast(`已删除人设: ${preset.name}`, "info");
                            }}
                            title="删除此自定义模板"
                          >
                            ✕
                          </span>
                        )}
                      </span>
                    );
                  })}
                </div>

                {showAddCustom && (
                  <div className="persona-custom-creator">
                    <input
                      type="text"
                      className="persona-custom-name-input"
                      placeholder="输入新模板名称 (如: 🎙️ 英语面试官)..."
                      value={newTemplateName}
                      onChange={(e) => setNewTemplateName(e.target.value)}
                    />
                    <button
                      type="button"
                      className="btn-primary"
                      onClick={handleSaveCustom}
                    >
                      保存模板
                    </button>
                  </div>
                )}
              </div>

              <div className="persona-textarea-wrap">
                <textarea
                  className="persona-textarea"
                  value={personaDraft}
                  onChange={(e) => {
                    setPersonaDraft(e.target.value);
                    if (personaError) setPersonaError("");
                  }}
                  onKeyDown={(e) => {
                    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                      e.preventDefault();
                      void savePersona();
                    }
                  }}
                  placeholder="输入助手 System Prompt 提示词..."
                  rows={8}
                  aria-label="系统提示词"
                />
                <div className="persona-meta-bar">
                  <span>支持快捷键 <kbd>Cmd / Ctrl + Enter</kbd> 快速保存</span>
                  <span>{personaDraft.length} 字符</span>
                </div>
                {personaError && <p className="persona-error">{personaError}</p>}
              </div>
            </div>

            <div className="persona-dialog-footer">
              <span style={{ fontSize: "0.74rem", color: "var(--text-muted)" }}>
                保存后立即向 LM Studio 下发并清空当前上下文
              </span>
              <div className="persona-dialog-footer-right">
                <button type="button" className="btn-ctrl" onClick={cancelPersona}>
                  取消
                </button>
                <button type="button" className="btn-primary" onClick={() => void savePersona()} disabled={!commandSocket.ready}>
                  应用并生效
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function useDisplayedAssistantPhase(phase: AssistantPhase): AssistantPhase {
  const [displayedPhase, setDisplayedPhase] = useState(phase);
  const phaseStartedAtRef = useRef(Date.now());

  useLayoutEffect(() => {
    if (phase === displayedPhase) return;

    const delay = getAssistantPhaseTransitionDelay(
      displayedPhase,
      phase,
      phaseStartedAtRef.current,
    );
    if (delay === 0) {
      phaseStartedAtRef.current = Date.now();
      setDisplayedPhase(phase);
      return;
    }

    const timer = window.setTimeout(() => {
      phaseStartedAtRef.current = Date.now();
      setDisplayedPhase(phase);
    }, delay);
    return () => window.clearTimeout(timer);
  }, [displayedPhase, phase]);

  return displayedPhase;
}

/**
 * 高性能声学动态波形绘制：
 * 1. 消除 Layout Thrashing（尺寸仅在 ResizeObserver 中更新并缓存）
 * 2. 状态感知智能降频（活跃态 35FPS，待命/静音态 15FPS）
 * 3. 集成 Page Visibility API（后台标签页彻底挂起，0 CPU/GPU 消耗）
 */
function AssistantWaveform({
  phase,
  isMuted,
}: {
  readonly phase: AssistantPhase;
  readonly isMuted: boolean;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const phaseRef = useRef(phase);
  const mutedRef = useRef(isMuted);
  phaseRef.current = phase;
  mutedRef.current = isMuted;

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    let animFrame = 0;
    let tick = 0;
    let lastTime = 0;
    let cachedWidth = 0;
    let cachedHeight = 0;
    let isRunning = true;

    const barCount = 34;
    const barLevels = new Array(barCount).fill(4);
    const targetLevels = new Array(barCount).fill(4);

    const resize = () => {
      const bounds = canvas.getBoundingClientRect();
      cachedWidth = bounds.width;
      cachedHeight = bounds.height;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.round(cachedWidth * dpr));
      canvas.height = Math.max(1, Math.round(cachedHeight * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const render = (currentTime: number) => {
      if (!isRunning) return;

      const currentPhase = phaseRef.current;
      const muted = mutedRef.current;
      const isActive = !muted
        && currentPhase !== "idle"
        && currentPhase !== "degraded"
        && currentPhase !== "stopped";

      // 动态帧率控制：活跃状态 ~35FPS (28ms)，待命/静音状态 ~15FPS (66ms)
      const targetInterval = isActive ? 28 : 66;
      const elapsed = currentTime - lastTime;

      if (elapsed >= targetInterval) {
        lastTime = currentTime - (elapsed % targetInterval);
        tick += 1;

        if (cachedWidth > 0 && cachedHeight > 0) {
          // 更新目标电平
          for (let i = 0; i < barCount; i++) {
            if (muted) {
              targetLevels[i] = 2;
            } else {
              switch (currentPhase) {
                case "idle":
                case "degraded":
                case "stopped":
                  targetLevels[i] = 3 + Math.sin(tick * 0.08 + i * 0.2) * 1.5;
                  break;
                case "listening":
                  targetLevels[i] =
                    4 + Math.random() * 24 + Math.sin(i * 0.3 + tick * 0.2) * 6;
                  break;
                case "thinking":
                  targetLevels[i] = 6 + Math.sin(tick * 0.2 - i * 0.4) * 12;
                  break;
                case "speaking":
                  targetLevels[i] =
                    6 +
                    Math.abs(Math.sin(tick * 0.15 + i * 0.25)) * 26 +
                    Math.random() * 8;
                  break;
              }
            }
          }

          ctx.clearRect(0, 0, cachedWidth, cachedHeight);

          const gradient = ctx.createLinearGradient(0, 0, cachedWidth, 0);
          if (muted) {
            gradient.addColorStop(0, "rgba(239, 68, 68, 0.4)");
            gradient.addColorStop(1, "rgba(239, 68, 68, 0.4)");
          } else if (currentPhase === "speaking") {
            gradient.addColorStop(0, "#6366f1");
            gradient.addColorStop(0.5, "#a855f7");
            gradient.addColorStop(1, "#06b6d4");
          } else if (currentPhase === "listening") {
            gradient.addColorStop(0, "#10b981");
            gradient.addColorStop(1, "#06b6d4");
          } else if (currentPhase === "thinking") {
            gradient.addColorStop(0, "#f59e0b");
            gradient.addColorStop(1, "#ec4899");
          } else {
            gradient.addColorStop(0, "rgba(148, 163, 184, 0.4)");
            gradient.addColorStop(1, "rgba(148, 163, 184, 0.6)");
          }

          ctx.fillStyle = gradient;

          const gap = cachedWidth / barCount;
          const barWidth = Math.max(2.5, gap * 0.52);

          for (let i = 0; i < barCount; i++) {
            barLevels[i] += (targetLevels[i] - barLevels[i]) * 0.25;
            const h = Math.max(2, barLevels[i]);
            const x = i * gap + (gap - barWidth) / 2;
            const y = (cachedHeight - h) / 2;

            ctx.beginPath();
            ctx.roundRect(x, y, barWidth, h, 2);
            ctx.fill();
          }
        }
      }

      animFrame = requestAnimationFrame(render);
    };

    const handleVisibilityChange = () => {
      if (document.hidden) {
        isRunning = false;
        cancelAnimationFrame(animFrame);
      } else {
        if (!isRunning) {
          isRunning = true;
          lastTime = performance.now();
          animFrame = requestAnimationFrame(render);
        }
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    resize();

    if (!document.hidden) {
      animFrame = requestAnimationFrame(render);
    } else {
      isRunning = false;
    }

    return () => {
      isRunning = false;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      observer.disconnect();
      cancelAnimationFrame(animFrame);
    };
  }, []);

  return (
    <canvas
      className="assistant-waveform-canvas"
      ref={canvasRef}
      role="img"
      aria-label="声学动态频谱波形"
    />
  );
}
