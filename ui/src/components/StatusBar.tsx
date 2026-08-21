import { useState, useEffect, useCallback } from "react";
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
  tts: "TTS 桥 (:8765)",
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

function ServiceLight({
  service,
  onRefresh,
}: {
  service: ServiceInfo;
  onRefresh: () => void;
}) {
  const displayName = SERVICE_DISPLAY_NAMES[service.name] || service.name;
  const statusText = STATUS_LABELS[service.status] || service.status;

  return (
    <button
      type="button"
      className="service-light-pill"
      onClick={onRefresh}
      title={`${displayName} - ${statusText}\n地址: ${service.url}\n(点击重新探活)`}
    >
      <span className={`light-dot dot-${service.status}`} aria-hidden="true" />
      <span className="light-label">{displayName}</span>
    </button>
  );
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

  return (
    <header className="status-bar">
      <div className="status-left">
        <div className="status-brand">
          <span className="status-logo-icon">🎙️</span>
          <h1 className="status-title">Voice Studio</h1>
        </div>
        <span className="status-badge-chip">Apple Silicon / MLX</span>

        {/* 全局互斥模式指示器 */}
        {isMeetingRecording ? (
          <span className="status-mode-pill mode-meeting" title="当前活跃模式：会议助手录制中（语音交互已自动挂起）">
            <span className="mode-pill-dot recording" />
            <span>会议录制中</span>
          </span>
        ) : phase === "speaking" ? (
          <span className="status-mode-pill mode-assistant" title="当前活跃模式：语音助手播报中">
            <span>🗣️ 助手播报中</span>
          </span>
        ) : phase === "listening" ? (
          <span className="status-mode-pill mode-assistant" title="当前活跃模式：语音助手聆听中">
            <span>👂 助手聆听中</span>
          </span>
        ) : phase === "thinking" ? (
          <span className="status-mode-pill mode-assistant" title="当前活跃模式：助手思考中">
            <span>🧠 助手思考中</span>
          </span>
        ) : (
          <span className="status-mode-pill mode-idle" title="当前系统处于待命就绪状态">
            <span>💤 系统待命</span>
          </span>
        )}

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
                height: micMuted ? "2px" : phase === "listening" ? "9px" : "4px",
              }}
            />
            <span
              className="vu-bar"
              style={{
                height: micMuted ? "2px" : phase === "listening" ? "10px" : "6px",
              }}
            />
            <span
              className="vu-bar"
              style={{
                height: micMuted ? "2px" : phase === "listening" ? "7px" : "3px",
              }}
            />
          </div>
          <span>{micMuted ? "已静音" : "16kHz"}</span>
        </button>
      </div>

      <div className="status-lights" aria-label="服务状态监控">
        <span className="service-light-pill" title={`控制 WebSocket: ${commandSocket.state}`}>
          <span className={`light-dot dot-${commandSocket.ready ? "ok" : "checking"}`} aria-hidden="true" />
          <span className="light-label">控制 WS</span>
        </span>
        <span className="service-light-pill" title={`交互管道: ${pipelineStatus}`}>
          <span className={`light-dot dot-${pipelineStatus === "running" ? "ok" : pipelineStatus === "error" ? "error" : "checking"}`} aria-hidden="true" />
          <span className="light-label">交互管道</span>
        </span>
        <span className="service-light-pill" title={`字幕代理: ${subtitleStatus}`}>
          <span className={`light-dot dot-${subtitleStatus === "connected" ? "ok" : subtitleStatus === "error" ? "error" : "checking"}`} aria-hidden="true" />
          <span className="light-label">字幕代理</span>
        </span>
        <span className="service-light-pill" title={`PostgreSQL 会议存储: ${storageHealth}`}>
          <span className={`light-dot dot-${storageHealth === "ok" ? "ok" : storageHealth === "degraded" ? "checking" : "error"}`} aria-hidden="true" />
          <span className="light-label">PG 存储</span>
        </span>
        {services.map((s) => (
          <ServiceLight
            key={s.name}
            service={s}
            onRefresh={() => fetchServices(true)}
          />
        ))}
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
