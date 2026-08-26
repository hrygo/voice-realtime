import { useState, useEffect, useCallback, useRef } from "react";
import { applyTheme, useUISettingsStore, type Theme } from "../stores/uiSettingsStore";
import { selectAssistantPhase, useAssistantStore } from "../stores/assistantStore";
import { useMeetingStore } from "../stores/meetingStore";
import { showToast } from "./Toast";
import type { CommandSocketApi } from "../hooks/useCommandSocket";
import type { RuntimeMode } from "../contracts/meetingContract";
import { apiUrl } from "../config/runtimeConfig";
import "./StatusBar.css";

type ServiceStatus = "ok" | "unreachable" | "timeout" | "error" | "checking";
type NetworkScope = "local" | "network";
type HealthRequirement = "required" | "not-required";
type HealthDisplayState = "normal" | "required-error" | "not-required";

interface ServiceInfo {
  name: string;
  status: ServiceStatus;
  url: string;
  workload?: string | null;
  ws_state?: string | null;
  reconnect_count?: number | null;
  last_event_age_ms?: number | null;
  dropped_chunks?: number | null;
  gap_count?: number | null;
}

interface ServicesResponse {
  services: ServiceInfo[];
  diagnostics?: unknown;
  network_scope?: NetworkScope;
}

interface HealthItem {
  id: string;
  name: string;
  status: ServiceStatus;
  requirement: HealthRequirement;
  displayState: HealthDisplayState;
  details?: string[];
}

const REQUIRED_HEALTH_ITEMS = {
  assistant: ["ws", "pipeline", "tts", "lm"],
  subtitles: ["ws", "subtitle", "wlk"],
  meeting: ["ws", "subtitle", "wlk", "storage", "lm"],
  idle: ["ws"],
} as const satisfies Record<RuntimeMode, readonly string[]>;

const SERVICE_DISPLAY_NAMES: Record<string, string> = {
  wlk: "WhisperLiveKit (:8001)",
  tts: "Qwen3-TTS 桥 (:8765)",
  lm: "LM Studio (:1234)",
};

const STATUS_LABELS: Record<ServiceStatus, string> = {
  ok: "运行正常",
  unreachable: "服务未启动",
  timeout: "连接超时",
  error: "服务异常",
  checking: "检测中",
};

const SERVICE_DIAGNOSTIC_COMMANDS: Record<string, string> = {
  wlk: "uv run vr-subtitles",
  tts: "scripts/run-bridge.sh",
  storage: "psql knowledge -f scripts/bootstrap-meeting-db.sql",
};

const THEME_LABELS: Record<Theme, string> = {
  light: "☀️",
  dark: "🌙",
  system: "💻",
};

const THEME_TITLES: Record<Theme, string> = {
  light: "当前：亮色主题 (点击切换)",
  dark: "当前：暗色主题 (点击切换)",
  system: "当前：跟随系统 (点击切换)",
};

const THEME_CYCLE: readonly Theme[] = ["dark", "light", "system"];

function browserNetworkScope(): NetworkScope {
  if (typeof window === "undefined") return "local";
  const hostname = window.location.hostname.toLowerCase();
  return ["localhost", "127.0.0.1", "::1"].includes(hostname) ? "local" : "network";
}

function normalizeNetworkScope(value: unknown): NetworkScope {
  return value === "network" || value === "local" ? value : browserNetworkScope();
}

export function classifyHealthState(
  status: ServiceStatus,
  requirement: HealthRequirement,
): HealthDisplayState {
  if (requirement === "not-required") return "not-required";
  return status === "ok" ? "normal" : "required-error";
}

function isHealthItemRequired(id: string, mode: RuntimeMode | undefined): boolean {
  const currentMode = mode ?? "idle";
  return REQUIRED_HEALTH_ITEMS[currentMode].some((requiredId) => requiredId === id);
}

function formatDiagnosticText(value: unknown): string {
  return typeof value === "string" && value.trim().length > 0 ? value : "未知";
}

function formatDiagnosticNumber(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "未知";
}

function serviceDiagnosticDetails(service: ServiceInfo): string[] {
  const details: string[] = [];
  if (service.workload !== undefined) {
    details.push(`语音工作负载：${formatDiagnosticText(service.workload)}`);
  }
  if (service.ws_state !== undefined) {
    details.push(`WebSocket 状态：${formatDiagnosticText(service.ws_state)}`);
  }
  if (service.reconnect_count !== undefined) {
    details.push(`重连次数：${formatDiagnosticNumber(service.reconnect_count)}`);
  }
  if (service.last_event_age_ms !== undefined) {
    details.push(`距最近事件：${formatDiagnosticNumber(service.last_event_age_ms)} ms`);
  }
  if (service.dropped_chunks !== undefined) {
    details.push(`丢弃音频块：${formatDiagnosticNumber(service.dropped_chunks)}`);
  }
  if (service.gap_count !== undefined) {
    details.push(`音频缺口：${formatDiagnosticNumber(service.gap_count)}`);
  }
  return details;
}

export function sessionElapsedSeconds(startedAt: string | null, nowMs = Date.now()): number {
  if (!startedAt) return 0;
  const started = Date.parse(startedAt);
  return Number.isFinite(started) ? Math.max(0, Math.floor((nowMs - started) / 1000)) : 0;
}

export type WorkspaceTab = "assistant" | "meeting" | "subtitles";

interface StatusBarProps {
  commandSocket: CommandSocketApi;
  onOpenShortcuts?: () => void;
  activeTab?: WorkspaceTab | null;
  pendingTab?: WorkspaceTab | null;
  reconciling?: boolean;
  switchError?: string | null;
  onTabChange?: (tab: WorkspaceTab) => void;
}

function ThemeToggle() {
  const theme = useUISettingsStore((s) => s.theme);
  const setTheme = useUISettingsStore((s) => s.setTheme);

  const cycle = useCallback(() => {
    const current = THEME_CYCLE.indexOf(theme);
    const next = THEME_CYCLE[(current + 1) % THEME_CYCLE.length];
    if (next) setTheme(next);
  }, [theme, setTheme]);

  return (
    <button
      type="button"
      className="status-icon-btn"
      onClick={cycle}
      aria-label={THEME_TITLES[theme]}
      title={THEME_TITLES[theme]}
    >
      {THEME_LABELS[theme]}
    </button>
  );
}

export default function StatusBar({
  commandSocket,
  onOpenShortcuts,
  activeTab = "assistant",
  pendingTab = null,
  reconciling = false,
  switchError = null,
  onTabChange,
}: StatusBarProps) {
  const [services, setServices] = useState<ServiceInfo[]>([
    { name: "wlk", status: "checking", url: "http://127.0.0.1:8001" },
    { name: "tts", status: "checking", url: "http://127.0.0.1:8765" },
    { name: "lm", status: "checking", url: "http://127.0.0.1:1234" },
  ]);
  const [networkScope, setNetworkScope] = useState<NetworkScope>(browserNetworkScope);
  const [sessionSeconds, setSessionSeconds] = useState(0);
  const [healthPopoverOpen, setHealthPopoverOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);

  const micMuted = useUISettingsStore((s) => s.micMuted);
  const pipelineStatus = useUISettingsStore((s) => s.pipelineStatus);
  const subtitleStatus = useUISettingsStore((s) => s.subtitleStatus);
  const storageHealth = useUISettingsStore((s) => s.storageHealth);
  const sessionStartedAt = useUISettingsStore((s) => s.sessionStartedAt);
  const phase = useAssistantStore(selectAssistantPhase);

  /** Session Timer */
  useEffect(() => {
    const update = () => {
      setSessionSeconds(sessionElapsedSeconds(sessionStartedAt));
    };
    update();
    const timer = setInterval(() => {
      update();
    }, 1000);
    return () => clearInterval(timer);
  }, [sessionStartedAt]);

  const setMicMuted = useCallback(async (muted: boolean, shortcut = false) => {
    if (!commandSocket.ready) {
      showToast("控制端连接中，麦克风状态未改变", "warning");
      return;
    }
    try {
      await commandSocket.sendCommand({ cmd: "set_mic_muted", muted });
      showToast(
        muted ? `已开启麦克风静音${shortcut ? " (M)" : ""}` : `已解除麦克风静音${shortcut ? " (M)" : ""}`,
        muted ? "warning" : "success",
      );
    } catch (error) {
      showToast(error instanceof Error ? error.message : "麦克风控制失败", "error");
    }
  }, [commandSocket]);

  const formatTimer = (totalSeconds: number) => {
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = totalSeconds % 60;
    if (h > 0) {
      return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    }
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  };

  /** 键盘快捷键监听：M 键麦克风静音切换 */
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      const activeEl = document.activeElement;
      const isInput =
        activeEl?.tagName === "INPUT" ||
        activeEl?.tagName === "TEXTAREA" ||
        (activeEl as HTMLElement)?.isContentEditable;
      if (isInput) return;

      if ((e.key === "m" || e.key === "M") && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey) {
        e.preventDefault();
        void setMicMuted(!micMuted, true);
      }
    },
    [micMuted, setMicMuted],
  );

  useEffect(() => {
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  /** Theme listener */
  useEffect(() => {
    const store = useUISettingsStore.getState();
    applyTheme(store.theme);

    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      const current = useUISettingsStore.getState();
      if (current.theme === "system") applyTheme("system");
    };
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  const fetchServices = useCallback(async (isManual = false) => {
    if (!isManual && typeof document !== "undefined" && document.hidden) return;
    try {
      const resp = await fetch(apiUrl("/api/services"));
      if (!resp.ok) return;
      const data: ServicesResponse = await resp.json();
      setNetworkScope(normalizeNetworkScope(data.network_scope));
      if (Array.isArray(data.services) && data.services.length > 0) {
        setServices(data.services);
        if (isManual) showToast("服务健康状态已刷新", "info");
      }
    } catch {
      if (isManual) showToast("服务探活请求失败", "warning");
    }
  }, []);

  useEffect(() => {
    fetchServices();
    const interval = setInterval(() => {
      if (!document.hidden) {
        fetchServices();
      }
    }, 8000);

    const handleVisibilityChange = () => {
      if (!document.hidden) {
        fetchServices();
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [fetchServices]);

  // Click outside to close health popover
  useEffect(() => {
    if (!healthPopoverOpen) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setHealthPopoverOpen(false);
      }
    };
    window.addEventListener("mousedown", handleClickOutside);
    return () => window.removeEventListener("mousedown", handleClickOutside);
  }, [healthPopoverOpen]);

  const meetingStatus = useMeetingStore((s) => s.status);
  const isMeetingRecording = meetingStatus === "recording" || meetingStatus === "finalizing";

  // Compute aggregate system health
  const storageStatus: ServiceStatus =
    storageHealth === "ok" ? "ok" : storageHealth === "degraded" ? "checking" : "error";
  const authoritativeMode = commandSocket.snapshot?.mode;
  const createHealthItem = (
    id: string,
    name: string,
    status: ServiceStatus,
    details?: string[],
  ): HealthItem => {
    const requirement: HealthRequirement = isHealthItemRequired(id, authoritativeMode)
      ? "required"
      : "not-required";
    return {
      id,
      name,
      status,
      requirement,
      displayState: classifyHealthState(status, requirement),
      details,
    };
  };

  const healthItems: HealthItem[] = [
    createHealthItem("ws", "控制 WebSocket", commandSocket.ready ? "ok" : "checking"),
    createHealthItem(
      "pipeline",
      "交互管道 (Pipecat)",
      pipelineStatus === "running" ? "ok" : pipelineStatus === "error" ? "error" : "checking",
    ),
    createHealthItem(
      "subtitle",
      "字幕代理 (SubtitleProxy)",
      subtitleStatus === "connected" ? "ok" : subtitleStatus === "error" ? "error" : "checking",
    ),
    createHealthItem("storage", "PostgreSQL 知识库", storageStatus),
    ...services.map((s): HealthItem => {
      const details = serviceDiagnosticDetails(s);
      return createHealthItem(s.name, SERVICE_DISPLAY_NAMES[s.name] || s.name, s.status, details);
    }),
  ];

  const orderedHealthItems = [...healthItems].sort((a, b) => {
    const priority = (item: HealthItem): number => {
      if (item.status === "checking") return 1;
      return item.displayState === "required-error" ? 0 : item.displayState === "normal" ? 1 : 2;
    };
    return priority(a) - priority(b);
  });
  const requiredHealthItems = healthItems.filter((item) => item.requirement === "required");
  const requiredOkCount = requiredHealthItems.filter((item) => item.status === "ok").length;
  const requiredCount = requiredHealthItems.length;
  const nonRequiredCount = healthItems.length - requiredCount;
  const hasRequiredError = requiredHealthItems.some(
    (item) => item.status !== "ok" && item.status !== "checking",
  );
  const hasRequiredChecking = requiredHealthItems.some((item) => item.status === "checking");
  const isAllOk = !hasRequiredError && !hasRequiredChecking && requiredOkCount === requiredCount;
  const switchTarget = pendingTab ?? (switchError ? activeTab : null);
  const switchTargetLabel = switchTarget === "subtitles"
    ? "实时字幕"
    : switchTarget === "meeting"
      ? "会议助手"
      : "语音助手";
  const switchAnnouncement = switchError
    || (reconciling ? "正在对账" : pendingTab ? `正在切换至${switchTargetLabel}` : "");

  return (
    <header className="status-bar">
      <div className="status-left">
        <div className="status-brand" title="Voice Studio">
          <span className="status-logo-icon" role="img" aria-label="Voice Studio">
            🎙️
          </span>
          <h1 className="status-title">
            <span className="title-full">Voice Studio</span>
            <span className="title-short">VS</span>
          </h1>
        </div>
        <span className="status-badge-chip">Apple Silicon / MLX</span>

        {/* 麦克风电平 & 静音控件 */}
        <button
          type="button"
          className={`mic-vu-widget ${micMuted ? "muted" : ""}`}
          onClick={() => {
            void setMicMuted(!micMuted);
          }}
          disabled={!commandSocket.ready}
          title={micMuted ? "麦克风已静音 (按 M 恢复)" : "麦克风采集中 (按 M 静音)"}
        >
          <span>{micMuted ? "🔇" : "🎙️"}</span>
          <div className="vu-bars" aria-hidden="true">
            <span
              className="vu-bar"
              style={{
                height: micMuted ? "2px" : phase === "listening" ? "10px" : "4px",
              }}
            />
            <span
              className="vu-bar"
              style={{
                height: micMuted ? "2px" : phase === "listening" ? "12px" : "7px",
              }}
            />
            <span
              className="vu-bar"
              style={{
                height: micMuted ? "2px" : phase === "listening" ? "8px" : "3px",
              }}
            />
          </div>
          <span className="mic-vu-label">{micMuted ? "已静音" : "16kHz"}</span>
        </button>
      </div>

      {/* 居中核心三种模式切换 (Segmented Control Navigation) */}
      {onTabChange && (
        <div className="status-center">
          <nav className="status-workspace-tabs" aria-label="工作区模式切换">
            <button
              type="button"
              className={`status-tab-btn tab-assistant ${activeTab === "assistant" ? "active" : ""} ${switchTarget === "assistant" ? (switchError ? "switch-error" : "pending") : ""}`}
              onClick={() => onTabChange("assistant")}
              disabled={pendingTab !== null || isMeetingRecording}
              aria-busy={switchTarget === "assistant" && !switchError ? true : undefined}
              title={
                isMeetingRecording
                  ? "会议录制中，语音交互已挂起（请先结束会议）"
                  : switchTarget === "assistant"
                    ? switchError || `正在切换至语音助手${reconciling ? "（对账中）" : ""}`
                    : "切换至语音助手 (快捷键 Cmd+1)"
              }
            >
              <span className="tab-icon">🤖</span>
              <span className="tab-label">
                <span className="tab-label-full">语音助手</span>
                <span className="tab-label-short">助手</span>
              </span>
              <kbd className="tab-kbd">⌘1</kbd>
              {isMeetingRecording && (
                <span className="tab-status-chip suspended" title="会议录制中，语音交互已挂起以防回声">
                  已挂起
                </span>
              )}
            </button>
            <button
              type="button"
              className={`status-tab-btn tab-meeting ${activeTab === "meeting" ? "active" : ""} ${switchTarget === "meeting" ? (switchError ? "switch-error" : "pending") : ""}`}
              onClick={() => onTabChange("meeting")}
              aria-busy={switchTarget === "meeting" && !switchError ? true : undefined}
              title={switchTarget === "meeting" ? switchError || `正在切换至会议助手${reconciling ? "（对账中）" : ""}` : "切换至会议助手 (快捷键 Cmd+2)"}
            >
              <span className="tab-icon">🎙️</span>
              <span className="tab-label">
                <span className="tab-label-full">会议助手</span>
                <span className="tab-label-short">会议</span>
              </span>
              <kbd className="tab-kbd">⌘2</kbd>
              {isMeetingRecording && (
                <span className="tab-status-chip recording" title="会议录制进行中">
                  <span className="tab-recording-dot" /> 录制中
                </span>
              )}
            </button>
            <button
              type="button"
              className={`status-tab-btn tab-subtitles ${activeTab === "subtitles" ? "active" : ""} ${switchTarget === "subtitles" ? (switchError ? "switch-error" : "pending") : ""}`}
              onClick={() => onTabChange("subtitles")}
              disabled={pendingTab !== null || isMeetingRecording}
              aria-busy={switchTarget === "subtitles" && !switchError ? true : undefined}
              title={
                isMeetingRecording
                  ? "会议录制中，请先结束会议再切换模式"
                  : switchTarget === "subtitles"
                    ? switchError || `正在切换至实时字幕${reconciling ? "（对账中）" : ""}`
                    : "切换至实时字幕 (快捷键 Cmd+3，已自动挂起 AI 助手以保证纯净转录)"
              }
            >
              <span className="tab-icon">📝</span>
              <span className="tab-label">
                <span className="tab-label-full">实时字幕</span>
                <span className="tab-label-short">字幕</span>
              </span>
              <kbd className="tab-kbd">⌘3</kbd>
            </button>
          </nav>
          {switchAnnouncement && (
            <span
              className="status-switch-announcement"
              role={switchError ? "alert" : "status"}
              aria-live="polite"
            >
              {switchAnnouncement}
            </span>
          )}
        </div>
      )}

      <div className="status-right">
        {/* 全局互斥模式指示器 (防抖固定尺寸胶囊) */}
        {(() => {
          let className = "mode-idle";
          let icon: React.ReactNode = "💤";
          let label = "系统待命";
          let title = "当前系统处于待命就绪状态";

          if (isMeetingRecording) {
            className = "mode-meeting";
            icon = <span className="mode-pill-dot recording" aria-hidden="true" />;
            label = "会议录制中";
            title = "当前活跃模式：会议助手录制中（语音交互已自动挂起）";
          } else if (phase === "speaking") {
            className = "mode-speaking";
            icon = "🗣️";
            label = "助手播报中";
            title = "当前活跃模式：语音助手播报中";
          } else if (phase === "listening") {
            className = "mode-listening";
            icon = "👂";
            label = "助手聆听中";
            title = "当前活跃模式：语音助手聆听中";
          } else if (phase === "thinking") {
            className = "mode-thinking";
            icon = "🧠";
            label = "助手思考中";
            title = "当前活跃模式：助手思考中";
          } else if (phase === "degraded") {
            className = "mode-degraded";
            icon = "⚠️";
            label = "系统受限";
            title = "当前语音服务处于降级或受限状态";
          }

          return (
            <span className={`status-mode-pill ${className}`} title={title}>
              <span className="mode-pill-indicator">{icon}</span>
              <span className="mode-pill-label">{label}</span>
            </span>
          );
        })()}

        {/* 系统健康中心 Popover 触发器 (右侧自然流) */}
        <div className="status-health-container" ref={popoverRef}>
          <button
            type="button"
            className={`health-master-pill ${isAllOk ? "all-ok" : hasRequiredError ? "has-error" : "checking"}`}
            onClick={() => setHealthPopoverOpen((prev) => !prev)}
            title="点击查看当前模式的服务健康状态与探活"
          >
            <span
              className={`light-dot ${isAllOk ? "dot-ok" : hasRequiredError ? "dot-error" : "dot-checking"}`}
              aria-hidden="true"
            />
            <span className="health-master-label">
              <span className="health-label-full">
                {isAllOk
                  ? `系统正常 (${requiredOkCount}/${requiredCount})`
                  : hasRequiredError
                    ? `核心组件异常 (${requiredOkCount}/${requiredCount})`
                    : `核心组件探活中 (${requiredOkCount}/${requiredCount})`}
              </span>
              <span className="health-label-short">
                {requiredOkCount}/{requiredCount}
              </span>
            </span>
            <span className="health-master-chevron">{healthPopoverOpen ? "▲" : "▼"}</span>
          </button>

          {healthPopoverOpen && (
            <div className="health-popover-dialog" role="dialog" aria-label="系统服务监控">
              <div className="health-popover-header">
                <div className="health-popover-title">
                  <span>🛡️ 系统服务健康状态</span>
                  <span className="health-summary-count">
                    {requiredOkCount}/{requiredCount} 核心组件
                    {nonRequiredCount > 0 ? ` · 非必需 ${nonRequiredCount} 项` : ""}
                  </span>
                </div>
                <button
                  type="button"
                  className="health-refresh-btn"
                  onClick={() => fetchServices(true)}
                  title="重新探活所有后端服务"
                >
                  🔄 刷新探活
                </button>
              </div>

              <div className="health-popover-list">
                {orderedHealthItems.map((item) => {
                  const visualState = item.requirement === "not-required"
                    ? "not-required"
                    : item.status === "checking"
                      ? "checking"
                      : item.displayState;
                  const statusLabel = item.requirement === "not-required"
                    ? "当前模式非必需"
                    : item.status === "ok"
                      ? "运行正常"
                      : item.status === "checking"
                        ? "检测中"
                        : "必须组件异常";
                  const diagnosticTitle = item.details?.join(" · ");
                  const title = item.requirement === "not-required"
                    ? diagnosticTitle
                      ? `当前模式不依赖此组件 · 当前状态：${STATUS_LABELS[item.status] || item.status} · ${diagnosticTitle}`
                      : `当前模式不依赖此组件 · 当前状态：${STATUS_LABELS[item.status] || item.status}`
                    : diagnosticTitle || STATUS_LABELS[item.status] || item.status;
                  return (
                  <div
                    key={item.id}
                    className={`health-popover-row state-${visualState}`}
                    title={title}
                  >
                    <div className="health-row-left">
                      <span className={`light-dot health-state-dot state-${visualState}`} aria-hidden="true" />
                      <span className="health-row-name">{item.name}</span>
                    </div>
                    <div className="health-row-right">
                      <span className={`health-row-status status-${visualState}`}>
                        {statusLabel}
                      </span>
                      {item.requirement === "required"
                        && item.status !== "ok"
                        && item.status !== "checking"
                        && SERVICE_DIAGNOSTIC_COMMANDS[item.id] && (
                        <button
                          type="button"
                          className="health-cmd-copy-btn"
                          onClick={(e) => {
                            e.stopPropagation();
                            const cmd = SERVICE_DIAGNOSTIC_COMMANDS[item.id];
                            navigator.clipboard.writeText(cmd).then(
                              () => showToast(`已复制启动命令: ${cmd}`, "success"),
                              () => showToast("复制失败", "error"),
                            );
                          }}
                          title={`复制终端启动命令: ${SERVICE_DIAGNOSTIC_COMMANDS[item.id]}`}
                        >
                          📋 复制命令
                        </button>
                      )}
                    </div>
                  </div>
                  );
                })}
              </div>

              <div className="health-popover-footer">
                <span className="health-footer-tip">
                  {networkScope === "local"
                    ? "全部服务运行于本地环回地址 (127.0.0.1)，数据不出本机"
                    : "服务可通过局域网访问，数据可能在局域网内传输"}
                </span>
              </div>
            </div>
          )}
        </div>

        <div className="session-timer" title="当前会话在线运行时长">
          <span>⏱️</span>
          <span>{formatTimer(sessionSeconds)}</span>
        </div>

        {onOpenShortcuts && (
          <button
            type="button"
            className="status-icon-btn"
            onClick={onOpenShortcuts}
            title="快捷键速查 (按 ?)"
            aria-label="快捷键速查"
          >
            ⌨️
          </button>
        )}

        <ThemeToggle />
      </div>
    </header>
  );
}
