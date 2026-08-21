import { useCallback, useEffect, useRef, useState } from "react";
import { useEventSocket } from "../hooks/useEventSocket";
import type { CommandSocketApi } from "../hooks/useCommandSocket";
import type { AssistantPhase, AssistantBubble } from "../stores/assistantStore";
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
    label: "外放保护",
    summary: "Agent 播报时不可打断",
    detail: "播报期间暂停麦克风输入，防止扬声器回声触发新一轮对话。",
    interruptionEnabled: false,
  },
  headphone_duplex: {
    icon: "🎧",
    label: "耳机双工",
    summary: "Agent 播报时可以插话",
    detail: "播报期间保持麦克风监听，检测到真人声音后立即打断 Agent。仅限佩戴耳机。",
    interruptionEnabled: true,
  },
};

function formatMetric(value: number | null): string {
  return value === null ? "—" : `${value}ms`;
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
  const transcript = useAssistantStore(selectAssistantTranscript);
  const connected = useAssistantStore(selectAssistantConnected);
  const lastInterruptionTime = useAssistantStore(selectLastInterruptionTime);
  const latestMetrics = useAssistantStore(selectAssistantLatestMetrics);
  const clearTranscript = useAssistantStore((state) => state.clearTranscript);

  const transcriptScrollRef = useRef<HTMLDivElement>(null);
  const [isScrolledUp, setIsScrolledUp] = useState(false);
  const [interruptionActive, setInterruptionActive] = useState(false);

  /* ---- 交互模式 (外放专注 vs 耳机双工打断) ---- */
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
    (mode: DuplexMode) => {
      if (mode === "headphone_duplex") {
        void sendCommandWith(
          { cmd: "set_duplex_mode", mode: "headphone_duplex" },
          "🎧 已开启【耳机打断模式】（高灵敏即时插话；⚠️ 仅限佩戴耳机时使用）",
        );
      } else {
        void sendCommandWith(
          { cmd: "set_duplex_mode", mode: "speaker_focus" },
          "🔊 已切换为【外放专注模式】（播报期间物理闭麦，彻底阻断扬声器回声）",
        );
      }
    },
    [sendCommandWith],
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

  const currentPhaseConfig = PHASE_CONFIG[phase];
  const allTemplates: readonly PersonaTemplate[] = [...BUILTIN_PERSONAS, ...customPersonas];

  return (
    <div className="assistant-workspace">
      {/* 左侧控制与人设侧边栏 */}
      <aside className="assistant-sidebar">
        <div className="assistant-sidebar-section">
          <div className="sidebar-section-header">
            <span className="sidebar-section-title">🎭 人设提示词库</span>
            <button
              type="button"
              className="sidebar-action-btn"
              onClick={openPersona}
              title="管理与定制人设提示词 (快捷键 Cmd+K)"
            >
              + 自定义
            </button>
          </div>

          <div className="persona-cards-list">
            {allTemplates.map((preset) => {
              const isSelected = persona.trim() === preset.prompt.trim();
              return (
                <div
                  key={preset.id}
                  className={`persona-sidebar-card ${isSelected ? "active" : ""}`}
                  onClick={() => void handleSelectPersona(preset.prompt, preset.name)}
                >
                  <div className="persona-card-header">
                    <span className="persona-card-name">{preset.name}</span>
                    {isSelected && <span className="persona-active-badge">✓ 生效中</span>}
                  </div>
                  <p className="persona-card-prompt">{preset.prompt}</p>
                </div>
              );
            })}
          </div>
        </div>

        <div className="assistant-sidebar-section">
          <span className="sidebar-section-title">🔊 TTS 播报音色</span>
          <div className="voice-select-wrap-sidebar">
            <select
              id="assistant-voice"
              className="voice-select-dropdown-sidebar"
              value={voice}
              onChange={(e) => void handleVoiceChange(e.target.value)}
              disabled={!commandSocket.ready}
            >
              {availableVoices.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </div>
        </div>

        {latestMetrics && (
          <div className="assistant-sidebar-section telemetry-section">
            <span className="sidebar-section-title">⚡ 交互时延监控</span>
            <div className="telemetry-grid">
              <div className="telemetry-item">
                <span className="telemetry-label">STT 识别</span>
                <span className="telemetry-val">{formatMetric(latestMetrics.sttMs)}</span>
              </div>
              <div className="telemetry-item">
                <span className="telemetry-label">LLM 首字 (TTFT)</span>
                <span className="telemetry-val">{formatMetric(latestMetrics.llmTtftMs)}</span>
              </div>
              <div className="telemetry-item">
                <span className="telemetry-label">TTS 首包 (TTFB)</span>
                <span className="telemetry-val">{formatMetric(latestMetrics.ttsTtfbMs)}</span>
              </div>
              <div className="telemetry-item highlight">
                <span className="telemetry-label">端到端总时延</span>
                <span className="telemetry-val">{formatMetric(latestMetrics.e2eMs)}</span>
              </div>
            </div>
          </div>
        )}

        <div className="assistant-sidebar-section controls-section">
          <span className="sidebar-section-title">🛠️ 会话控制</span>
          <div className="sidebar-ctrl-buttons">
            <button
              type="button"
              className="btn-sidebar-ctrl"
              onClick={() => sendCommand("clear_context")}
              disabled={!commandSocket.ready}
              title="清空 LLM 上下文记忆 (快捷键 Cmd+Shift+C)"
            >
              🧹 清空上下文
            </button>
            <button
              type="button"
              className="btn-sidebar-ctrl"
              onClick={() => {
                clearTranscript();
                showToast("对话记录已清空", "info");
              }}
              disabled={!transcript.length}
              title="清空屏幕对话记录"
            >
              🗑️ 清空屏幕
            </button>
            <button
              type="button"
              className="btn-sidebar-ctrl"
              onClick={() => sendCommand("restart")}
              disabled={!commandSocket.ready}
              title="重启后端交互管道"
            >
              🔄 重启管道
            </button>
            <button
              type="button"
              className="btn-sidebar-ctrl danger"
              onClick={() => sendCommand("stop_session")}
              disabled={!commandSocket.ready}
              title="停止语音会话"
            >
              ⏹️ 停止会话
            </button>
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

          <div className="duplex-mode-selector" role="radiogroup" aria-label="交互打断模式">
            <button
              type="button"
              className={`duplex-mode-btn ${duplexMode === "speaker_focus" ? "active speaker" : ""}`}
              onClick={() => handleDuplexModeChange("speaker_focus")}
              disabled={!commandSocket.ready}
              title="外放专注模式：播报期间物理闭麦，彻底阻断扬声器自回声与自打断（推荐外放使用）"
            >
              <span className="duplex-mode-icon">🔊</span>
              <span className="duplex-mode-label">外放 · 不可打断</span>
            </button>
            <button
              type="button"
              className={`duplex-mode-btn ${duplexMode === "headphone_duplex" ? "active headphone" : ""}`}
              onClick={() => handleDuplexModeChange("headphone_duplex")}
              disabled={!commandSocket.ready}
              title="耳机打断模式：高灵敏即时插话（⚠️ 仅限佩戴耳机时使用，扬声器外放可能引起自打断）"
            >
              <span className="duplex-mode-icon">🎧</span>
              <span className="duplex-mode-label">耳机 · 可插话</span>
              <span className="duplex-caution-badge">仅限耳机</span>
            </button>
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

        <div
          className={`duplex-mode-explainer mode-${duplexMode}`}
          role="status"
          aria-live="polite"
        >
          <span className="duplex-mode-current">
            {duplexPresentation.icon} 当前：{duplexPresentation.label}
          </span>
          <strong>{duplexPresentation.summary}</strong>
          <span>{duplexPresentation.detail}</span>
        </div>

        {/* 状态步骤指示栏 + 打断插话指示 */}
        <div className="assistant-phase-bar" role="status" aria-label="助手处理阶段">
          <div className={`phase-step-item ${phase === "listening" ? "active step-listening" : ""}`}>
            <span className="phase-step-icon">👂</span>
            <span>1. 聆听麦克风</span>
          </div>
          <div className={`phase-step-item ${phase === "thinking" ? "active step-thinking" : ""}`}>
            <span className="phase-step-icon">🧠</span>
            <span>2. LM Studio 推理</span>
          </div>
          <div className={`phase-step-item ${phase === "speaking" ? "active step-speaking" : ""}`}>
            <span className="phase-step-icon">🗣️</span>
            <span>3. Qwen3-TTS 播报</span>
          </div>

          {interruptionActive && duplexPresentation.interruptionEnabled && (
            <div className="interruption-alert-chip" role="alert">
              <span>⚡ 已响应插话打断 (耳机)</span>
            </div>
          )}
        </div>

        {/* 60FPS 声学动态可视化波形 */}
        <div className="assistant-waveform-container">
          <AssistantWaveform phase={phase} isMuted={micMuted} />
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
                    {bubble.role === "assistant" && bubble.final ? (
                      <MarkdownRenderer content={bubble.text} />
                    ) : (
                      <span>{bubble.text}</span>
                    )}
                    <div className="bubble-actions-group">
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
