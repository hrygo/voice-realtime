import { useState, useEffect, useCallback } from "react";
import AssistantPanel from "./components/AssistantPanel";
import StatusBar from "./components/StatusBar";
import SubtitleStream from "./components/SubtitleStream";
import MeetingPanel from "./components/meeting/MeetingPanel";
import ShortcutsModal from "./components/ShortcutsModal";
import { ToastContainer } from "./components/Toast";
import { useCommandSocket } from "./hooks/useCommandSocket";
import { useMeetingSocket } from "./hooks/useMeetingSocket";
import { useMeetingStore } from "./stores/meetingStore";
import "./App.css";

export type WorkspaceTab = "assistant" | "meeting" | "subtitles";

function ActiveMeetingMiniDock({
  title,
  elapsed,
  segmentsCount,
  latestText,
  micMuted,
  onToggleMic,
  onReturn,
}: {
  title: string;
  elapsed: number;
  segmentsCount: number;
  latestText: string;
  micMuted: boolean;
  onToggleMic: () => void;
  onReturn: () => void;
}) {
  const formatTime = (sec: number) => {
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  };

  return (
    <aside className="active-meeting-mini-dock" aria-label="后台正在录制的会议">
      <div
        className="mini-dock-left"
        onClick={onReturn}
        title="点击返回正在录制的会议工作台 (快捷键 Cmd+2)"
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onReturn();
          }
        }}
      >
        <span className="mini-dock-dot" />
        <div className="mini-dock-info">
          <div className="mini-dock-header-line">
            <span className="mini-dock-tag">会议录制中</span>
            <span className="mini-dock-title" title={title}>{title || "当前会议"}</span>
            <span className="mini-dock-timer">⏱️ {formatTime(elapsed)}</span>
          </div>
          <p className="mini-dock-snippet">
            {latestText ? `🎙️ ${latestText}` : `已记录 ${segmentsCount} 段发言...`}
          </p>
        </div>
      </div>
      <div className="mini-dock-actions">
        <button
          type="button"
          className={`mini-dock-mic-btn ${micMuted ? "muted" : ""}`}
          onClick={onToggleMic}
          title={micMuted ? "解除麦克风静音 (按 M)" : "将麦克风静音 (按 M)"}
        >
          {micMuted ? "🔇" : "🎤"}
        </button>
        <button
          type="button"
          className="mini-dock-return-btn"
          onClick={onReturn}
          title="返回会议工作台"
        >
          返回 ↗
        </button>
      </div>
    </aside>
  );
}

export default function App() {
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("assistant");
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const commandSocket = useCommandSocket();
  useMeetingSocket();

  const meetingStatus = useMeetingStore((s) => s.status);
  const sessionStartedAt = useMeetingStore((s) => s.sessionStartedAt);
  const activeMeeting = useMeetingStore((s) => s.activeMeeting);
  const segments = useMeetingStore((s) => s.segments);
  const partialText = useMeetingStore((s) => s.partialText);
  const meetingMicMuted = useMeetingStore((s) => s.health.mic_muted);
  const isMeetingRecording = meetingStatus === "recording" || meetingStatus === "finalizing";

  // Live elapsed time for meeting recording
  const [recordingElapsed, setRecordingElapsed] = useState(0);
  useEffect(() => {
    if (!isMeetingRecording || !sessionStartedAt) {
      setRecordingElapsed(0);
      return;
    }
    const startMs = Date.parse(sessionStartedAt);
    const tick = () => {
      const diff = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
      setRecordingElapsed(diff);
    };
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [isMeetingRecording, sessionStartedAt]);

  /** Tab 智能联动：切换到「实时字幕」时自动挂起 AI 助手；切回「语音助手」时自动恢复 */
  const handleTabChange = useCallback(
    (newTab: WorkspaceTab) => {
      setActiveTab(newTab);
      if (isMeetingRecording) {
        return; // 会议录制中由会议状态机接管
      }
      if (!commandSocket.ready) {
        return;
      }
      if (newTab === "subtitles") {
        void commandSocket.sendCommand({ cmd: "stop_session" }).catch(() => {});
      } else if (newTab === "assistant") {
        void commandSocket.sendCommand({ cmd: "start_assistant" }).catch(() => {});
      }
    },
    [commandSocket, isMeetingRecording],
  );

  // Global Keyboard Shortcuts (Cmd/Ctrl + 1/2/3 for tabs, ? for help)
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      const isInput = target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;

      if (!isInput) {
        if (e.key === "?" && !e.metaKey && !e.ctrlKey) {
          e.preventDefault();
          setShortcutsOpen((prev) => !prev);
        } else if ((e.metaKey || e.ctrlKey) && !e.shiftKey) {
          if (e.key === "1") {
            e.preventDefault();
            handleTabChange("assistant");
          } else if (e.key === "2") {
            e.preventDefault();
            handleTabChange("meeting");
          } else if (e.key === "3") {
            e.preventDefault();
            handleTabChange("subtitles");
          }
        }
      }
    },
    [handleTabChange],
  );

  useEffect(() => {
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  const latestSnippet = partialText || (segments.length > 0 ? segments[segments.length - 1].text : "");

  return (
    <div className="app-container">
      <StatusBar
        commandSocket={commandSocket}
        onOpenShortcuts={() => setShortcutsOpen(true)}
        activeTab={activeTab}
        onTabChange={handleTabChange}
        recordingElapsed={recordingElapsed}
      />

      <main className="app-main">
        {activeTab === "assistant" && (
          <div className="single-panel-layout">
            <AssistantPanel
              commandSocket={commandSocket}
              isMeetingRecording={isMeetingRecording}
              onNavigateMeeting={() => handleTabChange("meeting")}
            />
          </div>
        )}

        {activeTab === "meeting" && (
          <div className="single-panel-layout">
            <MeetingPanel commandSocket={commandSocket} />
          </div>
        )}

        {activeTab === "subtitles" && (
          <div className="single-panel-layout">
            <SubtitleStream
              isMeetingRecording={isMeetingRecording}
              onNavigateMeeting={() => handleTabChange("meeting")}
              commandSocket={commandSocket}
            />
          </div>
        )}
      </main>

      {/* 跨面板全局录制悬浮迷你浮岛 (Mini-Dock) */}
      {isMeetingRecording && activeTab !== "meeting" && (
        <ActiveMeetingMiniDock
          title={activeMeeting?.title || "当前会议"}
          elapsed={recordingElapsed}
          segmentsCount={segments.length}
          latestText={latestSnippet}
          micMuted={meetingMicMuted}
          onToggleMic={() => {
            void commandSocket.sendCommand({
              cmd: "set_mic_muted",
              muted: !meetingMicMuted,
            });
          }}
          onReturn={() => handleTabChange("meeting")}
        />
      )}

      <ShortcutsModal isOpen={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
      <ToastContainer />
    </div>
  );
}
