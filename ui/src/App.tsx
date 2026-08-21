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
  const isMeetingRecording = meetingStatus === "recording" || meetingStatus === "finalizing";

  // Global Keyboard Shortcuts
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    // If typing in an input or textarea, don't trigger global ?
    const target = e.target as HTMLElement;
    const isInput = target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;

    if (e.key === "?" && !isInput && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      setShortcutsOpen((prev) => !prev);
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
        >
          <span>🤖</span> 语音助手
        </button>
        <button
          type="button"
          className={`workspace-tab-btn ${activeTab === "meeting" ? "active" : ""}`}
          onClick={() => setActiveTab("meeting")}
        >
          <span>🎙️</span> 会议助手
          {isMeetingRecording && (
            <span
              style={{
                display: "inline-block",
                width: "8px",
                height: "8px",
                borderRadius: "50%",
                background: "var(--color-red)",
                marginLeft: "4px",
              }}
            />
          )}
        </button>
        <button
          type="button"
          className={`workspace-tab-btn ${activeTab === "subtitles" ? "active" : ""}`}
          onClick={() => setActiveTab("subtitles")}
        >
          <span>📝</span> 实时字幕
        </button>
      </nav>

      <main className="app-main">
        {activeTab === "assistant" && (
          <div className="assistant-layout">
            <div className="panel-wrapper">
              <AssistantPanel commandSocket={commandSocket} />
            </div>
            <div className="panel-wrapper subtitle-companion">
              <SubtitleStream />
            </div>
          </div>
        )}

        {activeTab === "meeting" && (
          <div className="single-panel-layout">
            <MeetingPanel commandSocket={commandSocket} />
          </div>
        )}

        {activeTab === "subtitles" && (
          <div className="single-panel-layout">
            <SubtitleStream />
          </div>
        )}
      </main>

      <ShortcutsModal isOpen={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
      <ToastContainer />
    </div>
  );
}
