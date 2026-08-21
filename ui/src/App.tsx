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

export default function App() {
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("assistant");
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const commandSocket = useCommandSocket();
  useMeetingSocket();
  const meetingStatus = useMeetingStore((s) => s.status);
  const sessionStartedAt = useMeetingStore((s) => s.sessionStartedAt);
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

  const formatTabTimer = (seconds: number) => {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  };

  // Global Keyboard Shortcuts (Cmd/Ctrl + 1/2/3 for tabs, ? for help)
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    const target = e.target as HTMLElement;
    const isInput = target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;

    if (!isInput) {
      if (e.key === "?" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        setShortcutsOpen((prev) => !prev);
      } else if ((e.metaKey || e.ctrlKey) && !e.shiftKey) {
        if (e.key === "1") {
          e.preventDefault();
          setActiveTab("assistant");
        } else if (e.key === "2") {
          e.preventDefault();
          setActiveTab("meeting");
        } else if (e.key === "3") {
          e.preventDefault();
          setActiveTab("subtitles");
        }
      }
    }
  }, []);

  useEffect(() => {
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  return (
    <div className="app-container">
      <StatusBar
        commandSocket={commandSocket}
        onOpenShortcuts={() => setShortcutsOpen(true)}
      />

      <nav className="workspace-tabs" aria-label="工作区切换">
        <button
          type="button"
          className={`workspace-tab-btn ${activeTab === "assistant" ? "active" : ""}`}
          onClick={() => setActiveTab("assistant")}
          title="切换至语音助手 (快捷键 Cmd+1)"
        >
          <span>🤖</span> 语音助手
          {isMeetingRecording && (
            <span className="tab-status-chip suspended" title="会议录制中，语音交互已挂起以防回声">
              已挂起
            </span>
          )}
        </button>
        <button
          type="button"
          className={`workspace-tab-btn ${activeTab === "meeting" ? "active" : ""}`}
          onClick={() => setActiveTab("meeting")}
          title="切换至会议助手 (快捷键 Cmd+2)"
        >
          <span>🎙️</span> 会议助手
          {isMeetingRecording && (
            <span className="tab-status-chip recording" title="会议录制进行中">
              <span className="tab-recording-dot" /> 录制中 {recordingElapsed > 0 && `(${formatTabTimer(recordingElapsed)})`}
            </span>
          )}
        </button>
        <button
          type="button"
          className={`workspace-tab-btn ${activeTab === "subtitles" ? "active" : ""}`}
          onClick={() => setActiveTab("subtitles")}
          title="切换至实时字幕 (快捷键 Cmd+3)"
        >
          <span>📝</span> 实时字幕
          {isMeetingRecording && (
            <span className="tab-status-chip sync" title="与会议转录同步中">
              同步中
            </span>
          )}
        </button>
      </nav>

      <main className="app-main">
        {activeTab === "assistant" && (
          <div className="single-panel-layout">
            <AssistantPanel
              commandSocket={commandSocket}
              isMeetingRecording={isMeetingRecording}
              onNavigateMeeting={() => setActiveTab("meeting")}
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
              onNavigateMeeting={() => setActiveTab("meeting")}
            />
          </div>
        )}
      </main>

      <ShortcutsModal isOpen={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
      <ToastContainer />
    </div>
  );
}
