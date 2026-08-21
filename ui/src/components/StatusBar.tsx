import { useState, useEffect, useCallback, useRef } from "react";
import { applyTheme, useUISettingsStore, type Theme } from "../stores/uiSettingsStore";
import { selectAssistantPhase, useAssistantStore } from "../stores/assistantStore";
import { useMeetingStore } from "../stores/meetingStore";
import { showToast } from "./Toast";
import type { CommandSocketApi } from "../hooks/useCommandSocket";
import "./StatusBar.css";

type ServiceStatus = "ok" | "unreachable" | "timeout" | "error" | "checking";

interface ServiceInfo {
  name: string;
  status: ServiceStatus;
  url: string;
}

interface ServicesResponse {
  services: ServiceInfo[];
}

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

export function sessionElapsedSeconds(startedAt: string | null, nowMs = Date.now()): number {
  if (!startedAt) return 0;
  const started = Date.parse(startedAt);
  return Number.isFinite(started) ? Math.max(0, Math.floor((nowMs - started) / 1000)) : 0;
}

interface StatusBarProps {
  commandSocket: CommandSocketApi;
  onOpenShortcuts?: () => void;
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

export default function StatusBar({ commandSocket, onOpenShortcuts }: StatusBarProps) {
  const [services, setServices] = useState<ServiceInfo[]>([
    { name: "wlk", status: "checking", url: "http://127.0.0.1:8001" },
    { name: "tts", status: "checking", url: "http://127.0.0.1:8765" },
    { name: "lm", status: "checking", url: "http://127.0.0.1:1234" },
  ]);
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
      const resp = await fetch("/api/services");
      if (!resp.ok) return;
      const data: ServicesResponse = await resp.json();
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

  // Global 'M' shortcut for Mute
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      const isInput =
        target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;
      if (!isInput && e.key.toLowerCase() === "m" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        void setMicMuted(!micMuted, true);
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [micMuted, setMicMuted]);

  const meetingStatus = useMeetingStore((s) => s.status);
  const isMeetingRecording = meetingStatus === "recording" || meetingStatus === "finalizing";

  // Compute aggregate system health (7 checkpoints)
  const healthItems = [
    {
      id: "ws",
      name: "控制 WebSocket",
      status: commandSocket.ready ? "ok" : "checking",
      detail: commandSocket.state,
    },
    {
      id: "pipeline",
      name: "交互管道 (Pipecat)",
      status: pipelineStatus === "running" ? "ok" : pipelineStatus === "error" ? "error" : "checking",
      detail: pipelineStatus,
    },
    {
      id: "subtitle",
      name: "字幕代理 (SubtitleProxy)",
      status: subtitleStatus === "connected" ? "ok" : subtitleStatus === "error" ? "error" : "checking",
      detail: subtitleStatus,
    },
    {
      id: "storage",
      name: "PostgreSQL 知识库",
      status: storageHealth === "ok" ? "ok" : storageHealth === "degraded" ? "checking" : "error",
      detail: storageHealth,
    },
    ...services.map((s) => ({
      id: s.name,
      name: SERVICE_DISPLAY_NAMES[s.name] || s.name,
      status: s.status,
      detail: `${s.url} (${STATUS_LABELS[s.status] || s.status})`,
    })),
  ];

  const okCount = healthItems.filter((h) => h.status === "ok").length;
  const totalCount = healthItems.length;
  const hasError = healthItems.some((h) => h.status === "error" || h.status === "unreachable");
  const isAllOk = okCount === totalCount;

  return (
    <header className="status-bar">
      <div className="status-left">
        <div className="status-brand">
          <span className="status-logo-icon">🎙️</span>
          <h1 className="status-title">Voice Studio</h1>
        </div>
        <span className="status-badge-chip">Apple Silicon / MLX</span>

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
          <span>{micMuted ? "已静音" : "16kHz"}</span>
        </button>
      </div>

      {/* 统一的系统健康中心 Popover 触发器 */}
      <div className="status-center-health" ref={popoverRef}>
        <button
          type="button"
          className={`health-master-pill ${isAllOk ? "all-ok" : hasError ? "has-error" : "checking"}`}
          onClick={() => setHealthPopoverOpen((prev) => !prev)}
          title="点击查看全系统 7 项服务健康状态与探活"
        >
          <span
            className={`light-dot ${isAllOk ? "dot-ok" : hasError ? "dot-error" : "dot-checking"}`}
            aria-hidden="true"
          />
          <span className="health-master-label">
            {isAllOk ? `引擎全就绪 (${okCount}/${totalCount})` : hasError ? `异常 (${okCount}/${totalCount})` : `探活中 (${okCount}/${totalCount})`}
          </span>
          <span className="health-master-chevron">{healthPopoverOpen ? "▲" : "▼"}</span>
        </button>

        {healthPopoverOpen && (
          <div className="health-popover-dialog" role="dialog" aria-label="系统服务监控">
            <div className="health-popover-header">
              <div className="health-popover-title">
                <span>🛡️ 系统服务健康状态</span>
                <span className="health-summary-count">
                  {okCount}/{totalCount} 项在线
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
              {healthItems.map((item) => (
                <div key={item.id} className="health-popover-row">
                  <div className="health-row-left">
                    <span className={`light-dot dot-${item.status}`} aria-hidden="true" />
                    <span className="health-row-name">{item.name}</span>
                  </div>
                  <span className={`health-row-status status-${item.status}`}>
                    {STATUS_LABELS[item.status as ServiceStatus] || item.status}
                  </span>
                </div>
              ))}
            </div>

            <div className="health-popover-footer">
              <span className="health-footer-tip">
                全部服务运行于本地环回地址 (127.0.0.1)，数据不出本机
              </span>
            </div>
          </div>
        )}
      </div>

      <div className="status-right">
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
