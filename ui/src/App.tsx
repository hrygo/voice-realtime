import { useState, useEffect, useCallback } from "react";
import AssistantPanel from "./components/AssistantPanel";
import StatusBar from "./components/StatusBar";
import SubtitleStream from "./components/SubtitleStream";
import ShortcutsModal from "./components/ShortcutsModal";
import { ToastContainer } from "./components/Toast";
import { useCommandSocket } from "./hooks/useCommandSocket";
import "./App.css";

export default function App() {
  const [activeTab, setActiveTab] = useState<"assistant" | "subtitles">("assistant");
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const commandSocket = useCommandSocket();

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

      <nav className="mobile-tabs" aria-label="移动端视图切换">
        <button
          type="button"
          className={`mobile-tab-btn ${activeTab === "assistant" ? "active" : ""}`}
          onClick={() => setActiveTab("assistant")}
        >
          <span>🤖</span> 语音助手
        </button>
        <button
          type="button"
          className={`mobile-tab-btn ${activeTab === "subtitles" ? "active" : ""}`}
          onClick={() => setActiveTab("subtitles")}
        >
          <span>📝</span> 实时字幕
        </button>
      </nav>

      <main className="app-main">
        <div className={`panel-wrapper ${activeTab === "assistant" ? "tab-active" : ""}`}>
          <AssistantPanel commandSocket={commandSocket} />
        </div>
        <div className={`panel-wrapper ${activeTab === "subtitles" ? "tab-active" : ""}`}>
          <SubtitleStream />
        </div>
      </main>

      <ShortcutsModal isOpen={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
      <ToastContainer />
    </div>
  );
}
