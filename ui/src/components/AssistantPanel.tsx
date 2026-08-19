import { useCallback, useEffect, useRef, useState } from "react";
import { useEventSocket } from "../hooks/useEventSocket";
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
  type PersonaTemplate,
} from "../stores/uiSettingsStore";
import { showToast } from "./Toast";
import "./AssistantPanel.css";

type Command = "clear_context" | "stop_session" | "restart";
type CommandState = "waiting" | "ready" | "sent";

const PHASE_CONFIG: Record<
  AssistantPhase,
  { label: string; icon: string; desc: string; className: string }
> = {
  idle: { label: "待命", icon: "💤", desc: "系统就绪，请直接说话", className: "phase-idle" },
  listening: { label: "聆听", icon: "👂", desc: "正在接收麦克风语音...", className: "phase-listening" },
  thinking: { label: "思考", icon: "🧠", desc: "LM Studio 推理生成中...", className: "phase-thinking" },
  speaking: { label: "播报", icon: "🗣️", desc: "Qwen3-TTS 语音播报中...", className: "phase-speaking" },
};

const FALLBACK_VOICES: readonly string[] = ["default", "warm", "bright", "calm"];

export default function AssistantPanel() {
  const phase = useAssistantStore(selectAssistantPhase);
  const transcript = useAssistantStore(selectAssistantTranscript);
  const connected = useAssistantStore(selectAssistantConnected);
  const lastInterruptionTime = useAssistantStore(selectLastInterruptionTime);
  const latestMetrics = useAssistantStore(selectAssistantLatestMetrics);
  const clearTranscript = useAssistantStore((state) => state.clearTranscript);

  const transcriptScrollRef = useRef<HTMLDivElement>(null);
  const commandSocketRef = useRef<WebSocket | null>(null);
  const [commandState, setCommandState] = useState<CommandState>("waiting");
  const [isScrolledUp, setIsScrolledUp] = useState(false);
  const [interruptionActive, setInterruptionActive] = useState(false);

  /* ---- 人格与人设管理 ---- */
  const [personaOpen, setPersonaOpen] = useState(false);
  const persona = useUISettingsStore((s) => s.persona);
  const setPersona = useUISettingsStore((s) => s.setPersona);
  const customPersonas = useUISettingsStore((s) => s.customPersonas);
  const addCustomPersona = useUISettingsStore((s) => s.addCustomPersona);
  const removeCustomPersona = useUISettingsStore((s) => s.removeCustomPersona);

  const [personaDraft, setPersonaDraft] = useState(persona);
  const [newTemplateName, setNewTemplateName] = useState("");
  const [showAddCustom, setShowAddCustom] = useState(false);
  const [personaError, setPersonaError] = useState("");

  /* ---- 音色选择 ---- */
  const voice = useUISettingsStore((s) => s.voice);
  const setVoice = useUISettingsStore((s) => s.setVoice);
  const micMuted = useUISettingsStore((s) => s.micMuted);
  const [availableVoices, setAvailableVoices] = useState<readonly string[]>(FALLBACK_VOICES);

  // 打断插话动效监听
  useEffect(() => {
    if (!lastInterruptionTime) return;
    setInterruptionActive(true);
    const timer = setTimeout(() => setInterruptionActive(false), 3500);
    return () => clearTimeout(timer);
  }, [lastInterruptionTime]);

  /** 发送通用命令 */
  const sendCommandWith = useCallback(
    (payload: Record<string, unknown>, toastMsg?: string) => {
      const commandSocket = commandSocketRef.current;
      if (!commandSocket || commandSocket.readyState !== WebSocket.OPEN) {
        showToast("控制端连接中，请稍候...", "warning");
        setCommandState("waiting");
        return;
      }

      try {
        commandSocket.send(JSON.stringify(payload));
        setCommandState("sent");
        if (toastMsg) showToast(toastMsg, "success");
      } catch (error) {
        console.warn("语音助手控制指令发送失败", error);
        showToast("指令发送失败", "error");
        setCommandState("waiting");
      }
    },
    [],
  );

  const sendCommand = useCallback(
    (command: Command) => {
      const msgMap: Record<Command, string> = {
        clear_context: "已清空 LLM 上下文记忆",
        stop_session: "已停止语音交互会话",
        restart: "已下发管道重启指令",
      };
      sendCommandWith({ cmd: command }, msgMap[command]);
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
  const savePersona = useCallback(() => {
    const trimmed = personaDraft.trim();
    if (!trimmed) {
      setPersonaError("系统提示词不能为空");
      return;
    }
    setPersona(trimmed);
    sendCommandWith({ cmd: "set_persona", prompt: trimmed }, "人格提示词已更新并生效");
    setPersonaOpen(false);
  }, [personaDraft, setPersona, sendCommandWith]);

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
    (value: string) => {
      const previous = voice;
      setVoice(value);
      const socket = commandSocketRef.current;
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        showToast("控制端未就绪，音色暂未同步", "warning");
        return;
      }
      try {
        socket.send(JSON.stringify({ cmd: "set_voice", voice: value }));
        showToast(`音色已切换为: ${value}`, "success");
      } catch {
        setVoice(previous);
        showToast("音色切换失败，已恢复原值", "error");
      }
    },
    [voice, setVoice],
  );

  /** 复制气泡文本 */
  const handleCopyBubble = useCallback((text: string) => {
    navigator.clipboard.writeText(text).then(
      () => showToast("已复制对话内容", "success"),
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
    if (!isScrolledUp) {
      const el = transcriptScrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }
  }, [transcript, isScrolledUp]);

  /** 控制通道建立 */
  useEffect(() => {
    let commandSocket: WebSocket | null = null;
    try {
      commandSocket = new WebSocket("/ws/assistant/cmd");
      commandSocketRef.current = commandSocket;
      commandSocket.onopen = () => setCommandState("ready");
      commandSocket.onclose = () => setCommandState("waiting");
      commandSocket.onerror = () => setCommandState("waiting");
    } catch (error) {
      console.warn("语音助手控制端接入中", error);
    }

    return () => {
      commandSocket?.close();
      commandSocketRef.current = null;
    };
  }, []);

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

  const currentPhaseConfig = PHASE_CONFIG[phase];
  const allTemplates: readonly PersonaTemplate[] = [...BUILTIN_PERSONAS, ...customPersonas];

  return (
    <section className="panel assistant-panel" aria-label="语音助手">
      {/* 头部 */}
      <header className="panel-header assistant-header">
        <div className="assistant-header-title-wrap">
          <h2>
            <span>🤖</span> 语音助手
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

      {/* 状态步骤指示栏 + 打断插话指示 */}
      <div className="assistant-phase-bar" role="status" aria-label="助手处理阶段">
        <div className={`phase-step-item ${phase === "listening" ? "active step-listening" : ""}`}>
          <span className="phase-step-icon">👂</span>
          <span>1. 聆听</span>
        </div>
        <div className={`phase-step-item ${phase === "thinking" ? "active step-thinking" : ""}`}>
          <span className="phase-step-icon">🧠</span>
          <span>2. 思考</span>
        </div>
        <div className={`phase-step-item ${phase === "speaking" ? "active step-speaking" : ""}`}>
          <span className="phase-step-icon">🗣️</span>
          <span>3. 播报</span>
        </div>

        {latestMetrics && (
          <div
            className="assistant-metrics-pill"
            title={`STT 耗时: ${latestMetrics.sttMs}ms | LLM 首字 (TTFT): ${latestMetrics.llmTtftMs}ms | TTS 首包: ${latestMetrics.ttsTtfbMs}ms | 端到端总时延: ${latestMetrics.e2eMs}ms`}
          >
            <span>⚡ #{latestMetrics.turnId}</span>
            <span>{latestMetrics.e2eMs}ms</span>
            <span className="metrics-detail-hint">
              (STT {latestMetrics.sttMs}ms · TTFT {latestMetrics.llmTtftMs}ms)
            </span>
          </div>
        )}

        {interruptionActive && (
          <div className="interruption-alert-chip" role="alert">
            <span>⚡ 已响应插话打断</span>
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
          {transcript.map((bubble: AssistantBubble, idx: number) => (
            <div
              className={`assistant-bubble-row ${bubble.role}`}
              key={`${bubble.role}-${bubble.turnId ?? idx}-${bubble.timestamp ?? idx}`}
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
                  <span className="bubble-interrupted-tag">⚡ 已打断</span>
                )}
              </div>
              <div className={`bubble-card ${bubble.final ? "final" : "streaming"}`}>
                <span>{bubble.text}</span>
                <div className="bubble-actions-group">
                  <button
                    type="button"
                    className="bubble-action-btn"
                    onClick={() => handleCopyBubble(bubble.text)}
                    title="复制内容"
                  >
                    📋
                  </button>
                </div>
              </div>
            </div>
          ))}

          {!transcript.length && (
            <div className="assistant-empty-state">
              <span className="empty-state-icon">🎙️</span>
              <p className="empty-state-title">等待语音输入...</p>
              <p className="empty-state-desc">
                直接对着麦克风说话，AI 助手将实时转写、推理并语音应答。支持随时插话打断。
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

      {/* 控制中心底部栏 */}
      <footer className="assistant-controls-footer">
        <button
          type="button"
          className="btn-ctrl"
          onClick={() => sendCommand("restart")}
          title="重启后端交互管道"
        >
          <span>🔄</span> 重启
        </button>
        <button
          type="button"
          className="btn-ctrl btn-ctrl-danger"
          onClick={() => sendCommand("stop_session")}
          title="停止当前语音会话"
        >
          <span>⏹️</span> 停止
        </button>
        <button
          type="button"
          className="btn-ctrl"
          onClick={() => sendCommand("clear_context")}
          title="清空 LLM 上下文记忆 (快捷键 Cmd+Shift+C)"
        >
          <span>🧹</span> 清空上下文
        </button>
        <button
          type="button"
          className="btn-ctrl"
          onClick={() => {
            clearTranscript();
            showToast("对话记录已清空", "info");
          }}
          disabled={!transcript.length}
          title="清空屏幕对话记录"
        >
          <span>🗑️</span> 清空记录
        </button>
        <button
          type="button"
          className="btn-ctrl"
          onClick={openPersona}
          title="管理与定制人设提示词 (快捷键 Cmd+K)"
        >
          <span>🎭</span> 人设库
        </button>

        {/* 音色下拉 */}
        <div className="voice-select-wrap">
          <label htmlFor="assistant-voice" className="voice-select-label">
            🔊
          </label>
          <select
            id="assistant-voice"
            className="voice-select-dropdown"
            value={voice}
            onChange={(e) => handleVoiceChange(e.target.value)}
          >
            {availableVoices.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </div>

        <span className="command-status-tag">
          {commandState === "ready" && "🟢 就绪"}
          {commandState === "sent" && "✓ 已下发"}
          {commandState === "waiting" && "🟡 连接中"}
        </span>
      </footer>

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
                      savePersona();
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
                <button type="button" className="btn-primary" onClick={savePersona}>
                  应用并生效
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

/**
 * 60FPS 升级版双层声学动态波形绘制
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
    const barCount = 34;
    const barLevels = new Array(barCount).fill(4);
    const targetLevels = new Array(barCount).fill(4);

    const resize = () => {
      const bounds = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.round(bounds.width * dpr));
      canvas.height = Math.max(1, Math.round(bounds.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const render = () => {
      const bounds = canvas.getBoundingClientRect();
      const currentPhase = phaseRef.current;
      const muted = mutedRef.current;
      tick += 1;

      if (tick % 4 === 0) {
        for (let i = 0; i < barCount; i++) {
          if (muted) {
            targetLevels[i] = 2;
          } else {
            switch (currentPhase) {
              case "idle":
                targetLevels[i] = 3 + Math.sin(tick * 0.05 + i * 0.2) * 2;
                break;
              case "listening":
                targetLevels[i] =
                  4 + Math.random() * 24 + Math.sin(i * 0.3 + tick * 0.1) * 6;
                break;
              case "thinking":
                targetLevels[i] = 6 + Math.sin(tick * 0.12 - i * 0.4) * 12;
                break;
              case "speaking":
                targetLevels[i] =
                  6 +
                  Math.abs(Math.sin(tick * 0.08 + i * 0.25)) * 26 +
                  Math.random() * 8;
                break;
            }
          }
        }
      }

      ctx.clearRect(0, 0, bounds.width, bounds.height);

      const gradient = ctx.createLinearGradient(0, 0, bounds.width, 0);
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

      const gap = bounds.width / barCount;
      const barWidth = Math.max(2.5, gap * 0.52);

      for (let i = 0; i < barCount; i++) {
        barLevels[i] += (targetLevels[i] - barLevels[i]) * 0.22;
        const h = Math.max(2, barLevels[i]);
        const x = i * gap + (gap - barWidth) / 2;
        const y = (bounds.height - h) / 2;

        ctx.beginPath();
        ctx.roundRect(x, y, barWidth, h, 2);
        ctx.fill();
      }

      animFrame = requestAnimationFrame(render);
    };

    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    resize();
    render();

    return () => {
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