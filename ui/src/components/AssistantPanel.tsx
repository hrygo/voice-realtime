import { useCallback, useEffect, useRef, useState } from "react";
import { useEventSocket } from "../hooks/useEventSocket";
import { runtimeConfig } from "../config/runtimeConfig";
import type { CommandSocketApi } from "../hooks/useCommandSocket";
import type { PCMOwner } from "../contracts/meetingContract";
import {
  parseAssistantEvent,
  type AssistantPhase,
  selectAssistantConnected,
  selectAssistantErrorMessage,
  selectAssistantLatestMetrics,
  selectAssistantPhase,
  selectAssistantSpeechSequence,
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
import { playAudioBlob } from "../utils/audioPlayback";
import { copyTextToClipboard } from "../utils/clipboard";
import { useDisplayedAssistantPhase } from "./AssistantPhaseDisplay";
import { AssistantWaveform as ExtractedAssistantWaveform } from "./AssistantWaveform";
import { AssistantTranscript } from "./AssistantTranscript";
import { AssistantErrorNotice } from "./AssistantErrorNotice";
import {
  canRequestDuplexModeChange,
  DEFAULT_SYSTEM_VOICES,
  DUPLEX_MODE_PRESENTATION,
  formatMetric,
  getDuplexModeFeedback,
  getDuplexToggleMode,
  getTelemetryBadge,
  PHASE_CONFIG,
  TELEMETRY_HELP_STEPS,
  type VoiceCatalogItem,
  VOICE_CONFIGS,
} from "./assistantPresentation";
import { PersonaDialog } from "./PersonaDialog";
import { VoiceDesignModal } from "./VoiceDesignModal";
import { showToast } from "./Toast";
import { apiUrl } from "../config/runtimeConfig";
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

/**
 * 给“用户开始说话”留出一个可感知的视觉窗口。
 * 实际管道状态仍按事件即时更新，这个延迟只作用于助手面板的展示层。
 */
export {
  getAssistantPhaseTransitionDelay,
  LISTENING_TO_THINKING_MIN_VISIBLE_MS,
} from "./AssistantPhaseDisplay";
export {
  canRequestDuplexModeChange,
  DUPLEX_MODE_PRESENTATION,
  getDuplexModeFeedback,
  getDuplexToggleMode,
  getTelemetryBadge,
  TELEMETRY_HELP_STEPS,
} from "./assistantPresentation";

export interface AssistantInputPresentation {
  readonly label: string;
  readonly detail: string;
}

export function getAssistantInputPresentation(
  _phase: AssistantPhase,
  micMuted: boolean,
  pcmOwner: PCMOwner | null | undefined,
): AssistantInputPresentation {
  if (pcmOwner === "subtitles") {
    return {
      label: "实时字幕占用音频",
      detail: "当前实际音频所有者是实时字幕，助手未接收麦克风语音。",
    };
  }
  if (pcmOwner === "meeting") {
    return {
      label: "会议采集占用音频",
      detail: "当前实际音频所有者是会议助手，助手未接收麦克风语音。",
    };
  }
  if (pcmOwner !== "assistant") {
    return {
      label: "助手未启动",
      detail: "语音助手尚未取得麦克风采集所有权，未开始接收语音。",
    };
  }
  if (micMuted) {
    return {
      label: "麦克风已静音",
      detail: "语音助手已取得麦克风所有权，但麦克风已静音，暂不接收语音。",
    };
  }
  return {
    label: "聆听麦克风",
    detail: "正在接收麦克风语音。",
  };
}

interface AssistantPhaseBadgePresentation {
  readonly label: string;
  readonly icon: string;
  readonly desc: string;
  readonly className: string;
}

export function getAssistantPhaseBadgePresentation(
  phase: AssistantPhase,
  micMuted: boolean,
  pcmOwner: PCMOwner | null | undefined,
): AssistantPhaseBadgePresentation {
  const phaseConfig = PHASE_CONFIG[phase];
  if (phase !== "listening" || (pcmOwner === "assistant" && !micMuted)) {
    return phaseConfig;
  }
  const inputPresentation = getAssistantInputPresentation(phase, micMuted, pcmOwner);
  return {
    ...phaseConfig,
    label: inputPresentation.label,
    icon: inputPresentation.label === "麦克风已静音" ? "🔇" : "⏹️",
    desc: inputPresentation.detail,
    className: "phase-idle",
  };
}

const DUPLEX_MODES: readonly DuplexMode[] = ["speaker_focus", "headphone_duplex"];

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
  const speechSequence = useAssistantStore(selectAssistantSpeechSequence);
  const visiblePhase = useDisplayedAssistantPhase(phase, speechSequence);
  const transcript = useAssistantStore(selectAssistantTranscript);
  const connected = useAssistantStore(selectAssistantConnected);
  const errorMessage = useAssistantStore(selectAssistantErrorMessage);
  const lastInterruptionTime = useAssistantStore(selectLastInterruptionTime);
  const latestMetrics = useAssistantStore(selectAssistantLatestMetrics);
  const telemetryBadge = getTelemetryBadge(latestMetrics);
  const clearTranscript = useAssistantStore((state) => state.clearTranscript);
  const clearAssistantError = useAssistantStore((state) => state.clearError);

  const transcriptScrollRef = useRef<HTMLDivElement>(null);
  const telemetryHelpRef = useRef<HTMLDivElement>(null);
  const [isScrolledUp, setIsScrolledUp] = useState(false);
  const [interruptionActive, setInterruptionActive] = useState(false);
  const [telemetryHelpOpen, setTelemetryHelpOpen] = useState(false);
  const [textInput, setTextInput] = useState("");
  const [lastSubmittedText, setLastSubmittedText] = useState("");
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
  const [availableVoices, setAvailableVoices] = useState<readonly VoiceCatalogItem[]>(DEFAULT_SYSTEM_VOICES);
  const [isPreviewPlaying, setIsPreviewPlaying] = useState(false);
  const [showVoiceDesignModal, setShowVoiceDesignModal] = useState(false);
  const currentVoiceItem = availableVoices.find((v) => v.id === voice);

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
        const res = await fetch(apiUrl("/v1/audio/speech"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            model: "speechrail/qwen3-tts",
            input: previewText,
            voice: v,
            response_format: "wav",
          }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        blob = await res.blob();
      } catch (err) {
        setIsPreviewPlaying(false);
        showToast("试听请求失败，请确保 SpeechRail 已启动", "error");
        return;
      }

      try {
        await playAudioBlob(blob);
      } catch {
        showToast("音频播放被浏览器拦截，请点击页面后重试", "error");
      } finally {
        setIsPreviewPlaying(false);
      }
    },
    [isPreviewPlaying],
  );

  /** 删除自定义音色 */
  const handleDeleteVoice = useCallback(
    async (targetVoiceId: string) => {
      const targetVoice = availableVoices.find((v) => v.id === targetVoiceId);
      if (!targetVoice || targetVoice.is_system) return;
      if (!window.confirm(`确定要删除自定义音色「${targetVoice.name}」吗？`)) return;

      try {
        const resp = await fetch(apiUrl(`/v1/voices/${targetVoiceId}`), {
          method: "DELETE",
        });
        if (!resp.ok) {
          throw new Error(`删除失败 (HTTP ${resp.status})`);
        }
        showToast(`已删除音色「${targetVoice.name}」`, "info");
        setAvailableVoices((prev) => prev.filter((v) => v.id !== targetVoiceId));
        if (voice === targetVoiceId) {
          void handleVoiceChange("default");
        }
      } catch (err) {
        showToast(err instanceof Error ? err.message : "删除音色失败", "error");
      }
    },
    [availableVoices, voice, handleVoiceChange],
  );

  /** 自定义音色创建完成回调 */
  const handleVoiceCreated = useCallback(
    (newVoice: VoiceCatalogItem) => {
      setAvailableVoices((prev) => {
        const exists = prev.some((v) => v.id === newVoice.id);
        return exists ? prev : [...prev, newVoice];
      });
      setShowVoiceDesignModal(false);
      void handleVoiceChange(newVoice.id);
    },
    [handleVoiceChange],
  );

  /** 文字兜底发送 */
  const handleSendText = async () => {
    const trimmed = textInput.trim();
    if (!trimmed) return;
    if (!commandSocket.ready) {
      showToast("控制端连接中，请稍候...", "warning");
      return;
    }
    setLastSubmittedText(trimmed);
    clearAssistantError();
    setTextInput("");
    const success = await sendCommandWith(
      { cmd: "send_text", text: trimmed },
      "已发送输入文本",
    );
    if (!success) {
      setTextInput(trimmed);
    }
  };

  /** 回填最近一次已提交的输入，等待用户显式再次发送。 */
  const handleRetryInput = useCallback(() => {
    const retryText = lastSubmittedText.trim();
    if (!retryText) return;
    setTextInput(retryText);
  }, [lastSubmittedText]);

  /** 导出对话 */
  const handleExportChat = (format: "md" | "txt") => {
    if (!transcript.length) {
      showToast("暂无对话记录可导出", "warning");
      return;
    }
    let content = "";
    if (format === "md") {
      content = `# Sona 语音交互记录\n\n- 日期: ${new Date().toLocaleString()}\n- 提示词人格: ${persona}\n- 音色: ${voice}\n\n---\n\n` +
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
        const res = await fetch(apiUrl("/v1/audio/speech"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            model: "speechrail/qwen3-tts",
            input: text.slice(0, 500),
            voice,
            response_format: "wav",
          }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        await playAudioBlob(blob);
      } catch {
        showToast("语音朗读请求失败，请确保 SpeechRail 已启动", "error");
      } finally {
        setPlayingBubbleKey(null);
      }
    },
    [playingBubbleKey, voice],
  );

  /** 复制气泡文本 */
  const handleCopyBubble = useCallback((text: string, key: string) => {
    void copyTextToClipboard(text).then(
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
      if (event) {
        useAssistantStore.getState().applyEvent(event);
        if (event.type === "stt" && event.state === "final" && event.text.trim()) {
          setLastSubmittedText(event.text.trim());
        }
        if (event.type === "system" && event.state === "pipeline_error") {
          showToast(
            event.message
              ? `语音管道错误：${event.message}`
              : "语音管道错误，请检查 SpeechRail / LM Studio 服务状态",
            "error",
          );
        }
      }
    } catch {
      // Ignore
    }
  }, []);

  const { state: socketState } = useEventSocket(runtimeConfig.assistantWsUrl, handleMessage);

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
    fetch(apiUrl("/v1/voices"))
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<{
          data?: Array<{
            id?: unknown;
            name?: unknown;
            instruction?: unknown;
            is_system?: unknown;
            created_at?: unknown;
            available?: unknown;
          }>;
          available?: unknown;
        }>;
      })
      .then((data) => {
        if (cancelled) return;
        if (Array.isArray(data.data) && data.data.length > 0) {
          const items: VoiceCatalogItem[] = data.data
            .filter((item) => item.available !== false && typeof item.id === "string")
            .map((item) => ({
              id: item.id as string,
              name: typeof item.name === "string" ? item.name : (item.id as string),
              instruction: typeof item.instruction === "string" ? item.instruction : undefined,
              is_system: item.is_system !== false,
              created_at: typeof item.created_at === "number" ? item.created_at : undefined,
              available: item.available !== false,
            }));
          if (items.length > 0) {
            setAvailableVoices(items);
          }
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
      const activeEl = document.activeElement as HTMLElement | null;
      const isInput =
        activeEl?.tagName === "INPUT" ||
        activeEl?.tagName === "TEXTAREA" ||
        activeEl?.isContentEditable;

      const isCmdOrCtrl = e.metaKey || e.ctrlKey;
      const isK = e.key.toLowerCase() === "k" || e.code === "KeyK";
      const isC = e.key.toLowerCase() === "c" || e.code === "KeyC";
      const isE = e.key.toLowerCase() === "e" || e.code === "KeyE";

      if (!isInput && isCmdOrCtrl && !e.altKey && !e.shiftKey && isK) {
        e.preventDefault();
        setPersonaOpen((prev) => !prev);
      } else if (!isInput && isCmdOrCtrl && e.shiftKey && isC) {
        e.preventDefault();
        sendCommand("clear_context");
      } else if (!isInput && isCmdOrCtrl && e.shiftKey && isE) {
        e.preventDefault();
        handleExportChat("md");
      }
    };
    window.addEventListener("keydown", handleGlobalShortcuts);
    return () => window.removeEventListener("keydown", handleGlobalShortcuts);
  }, [handleExportChat, sendCommand]);

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

  const inputPresentation = getAssistantInputPresentation(
    visiblePhase,
    micMuted,
    commandSocket.snapshot?.pcm_owner,
  );
  const currentPhaseConfig = getAssistantPhaseBadgePresentation(
    visiblePhase,
    micMuted,
    commandSocket.snapshot?.pcm_owner,
  );
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
              <div className="sidebar-field-badges-wrap">
                {VOICE_CONFIGS[voice] ? (
                  <span className="sidebar-field-badge">
                    {VOICE_CONFIGS[voice].tag}
                  </span>
                ) : currentVoiceItem ? (
                  <span className="sidebar-field-badge sidebar-field-badge-custom">
                    {currentVoiceItem.is_system ? "预置" : "自建"}
                  </span>
                ) : null}
              </div>
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
                  <optgroup label="🌟 系统预置音色">
                    {availableVoices
                      .filter((v) => v.is_system)
                      .map((v) => {
                        const desc = VOICE_CONFIGS[v.id];
                        return (
                          <option key={v.id} value={v.id}>
                            {desc ? `${desc.label} (${v.id})` : `${v.name} (${v.id})`}
                          </option>
                        );
                      })}
                  </optgroup>
                  {availableVoices.some((v) => !v.is_system) && (
                    <optgroup label="✨ 自定义设计音色">
                      {availableVoices
                        .filter((v) => !v.is_system)
                        .map((v) => (
                          <option key={v.id} value={v.id}>
                            {v.name} ({v.id})
                          </option>
                        ))}
                    </optgroup>
                  )}
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
              <button
                type="button"
                className="btn-voice-design-trigger"
                onClick={() => setShowVoiceDesignModal(true)}
                title="自然语言设计专属音色"
              >
                <span>✨ 定制</span>
              </button>
              {currentVoiceItem && !currentVoiceItem.is_system && (
                <button
                  type="button"
                  className="btn-voice-delete-trigger"
                  onClick={() => void handleDeleteVoice(voice)}
                  title={`删除自定义音色「${currentVoiceItem.name}」`}
                >
                  <TrashIcon size={13} />
                </button>
              )}
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
                  <strong id="telemetry-help-title">断句后首个语音包</strong>
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
                后端用单调时钟记录帧到达时间；这些数值从断句完成开始，只统计首个可观测帧，不估算未到达的阶段。STT 显示 0ms 不代表模型识别耗时为 0。
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
                  <strong>断句后首包响应（E2E）</strong>
                  <code>转写等待 + LLM 首字 + TTS 首包</code>
                </div>
                <span>从断句完成到首个 TTS 音频帧送入交互管道的可观测等待；三段数据缺少任一关键帧则显示“数据不足”。</span>
              </div>
              <p className="telemetry-help-note">
                不包含用户说话持续时间、LLM 完整生成时间或设备扬声器播放延迟。
              </p>
            </div>
          )}

          {latestMetrics ? (
            <div className="telemetry-compact-flow">
              <div className="flow-step">
                <span className="step-name">转写等待</span>
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
              <span>对话后实时呈现断句后首包响应遥测</span>
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
        {/* 一体化高效生态交互 Hero 头部 */}
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

          {/* 居中生态流体阶段流 */}
          <div className="assistant-phase-bar" role="status" aria-label="助手处理阶段">
            <div className={`phase-step-item ${visiblePhase === "listening" ? "active step-listening" : ""}`}>
              <span className="phase-step-icon">👂</span>
              <span>{inputPresentation.label}</span>
            </div>
            <span className="phase-flow-arrow" aria-hidden="true">→</span>
            <div className={`phase-step-item ${visiblePhase === "thinking" ? "active step-thinking" : ""}`}>
              <span className="phase-step-icon">🧠</span>
              <span>LM Studio 推理</span>
            </div>
            <span className="phase-flow-arrow" aria-hidden="true">→</span>
            <div className={`phase-step-item ${visiblePhase === "speaking" ? "active step-speaking" : ""}`}>
              <span className="phase-step-icon">🗣️</span>
              <span>Qwen3-TTS 播报</span>
            </div>
          </div>

          <div className="assistant-stage-header-right">
            {interruptionActive && duplexPresentation.interruptionEnabled && (
              <span className="interruption-alert-chip" role="alert" title="已成功响应耳机插话打断并停止 TTS 播报">
                <span className="interruption-pulse-dot" aria-hidden="true" />
                <span>已响应插话打断</span>
              </span>
            )}

            <div
              className={`assistant-phase-badge ${currentPhaseConfig.className}`}
              title={currentPhaseConfig.desc}
            >
              <span>{currentPhaseConfig.icon}</span>
              <span>{currentPhaseConfig.label}</span>
            </div>
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

        {/* 专业 60FPS 拟真声学多谐波波形 */}
        <div className="assistant-waveform-container">
          <ExtractedAssistantWaveform
            phase={visiblePhase}
            isMuted={micMuted}
            activeTextTrigger={transcript}
          />
        </div>

        {errorMessage && (
          <AssistantErrorNotice
            message={errorMessage}
            retryText={lastSubmittedText}
            onRetry={handleRetryInput}
          />
        )}


        <AssistantTranscript
          transcript={transcript}
          scrollRef={transcriptScrollRef}
          isScrolledUp={isScrolledUp}
          copiedKey={copiedKey}
          playingBubbleKey={playingBubbleKey}
          textInput={textInput}
          duplexSummary={duplexPresentation.summary}
          onScroll={handleScroll}
          onScrollToBottom={scrollToBottom}
          onReplay={handleReplayBubbleVoice}
          onCopy={handleCopyBubble}
          onTextInputChange={setTextInput}
          onSendText={handleSendText}
        />
      </main>

      {personaOpen && (
        <PersonaDialog
          templates={allTemplates}
          draft={personaDraft}
          error={personaError}
          showAddCustom={showAddCustom}
          newTemplateName={newTemplateName}
          commandReady={commandSocket.ready}
          onDraftChange={setPersonaDraft}
          onClearError={() => setPersonaError("")}
          onToggleAddCustom={() => setShowAddCustom((value) => !value)}
          onNewTemplateNameChange={setNewTemplateName}
          onSaveCustom={handleSaveCustom}
          onRemoveCustom={removeCustomPersona}
          onCancel={cancelPersona}
          onSave={savePersona}
        />
      )}

      {showVoiceDesignModal && (
        <VoiceDesignModal
          onCancel={() => setShowVoiceDesignModal(false)}
          onCreated={handleVoiceCreated}
        />
      )}
    </div>
  );
}
