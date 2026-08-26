import { useState, useEffect, useCallback, useRef } from "react";
import AssistantPanel from "./components/AssistantPanel";
import StatusBar from "./components/StatusBar";
import SubtitleStream from "./components/SubtitleStream";
import MeetingPanel from "./components/meeting/MeetingPanel";
import ShortcutsModal from "./components/ShortcutsModal";
import { showToast, ToastContainer } from "./components/Toast";
import { useCommandSocket } from "./hooks/useCommandSocket";
import { useMeetingSocket } from "./hooks/useMeetingSocket";
import type { RuntimeMode } from "./contracts/meetingContract";
import type { RuntimeStateSnapshot } from "./protocol";
import { useMeetingStore } from "./stores/meetingStore";
import "./App.css";

export type WorkspaceTab = "assistant" | "meeting" | "subtitles";

const WORKSPACE_TAB_STORAGE_KEY = "voice-studio:workspace-tab";

function readStoredWorkspaceTab(): WorkspaceTab {
  try {
    const stored = window.localStorage.getItem(WORKSPACE_TAB_STORAGE_KEY);
    if (stored === "assistant" || stored === "meeting" || stored === "subtitles") {
      return stored;
    }
  } catch {
    // localStorage 不可用时回退到默认工作区。
  }
  return "assistant";
}

function persistWorkspaceTab(tab: WorkspaceTab): void {
  try {
    window.localStorage.setItem(WORKSPACE_TAB_STORAGE_KEY, tab);
  } catch {
    // 隐私模式或存储受限时仅保留当前内存状态。
  }
}

export function resolveWorkspaceTab(
  mode: RuntimeMode,
  persistedTab: WorkspaceTab,
  currentTab: WorkspaceTab | null,
): WorkspaceTab {
  if (mode === "meeting") return "meeting";
  if (mode === "subtitles") return "subtitles";
  const candidate = currentTab ?? persistedTab;
  return candidate === "subtitles" ? "assistant" : candidate;
}

interface PendingWorkspaceSwitch {
  readonly tab: Exclude<WorkspaceTab, "meeting">;
  readonly startedRevision: number;
  readonly generation: number;
}

function errorCode(error: unknown): string | null {
  if (typeof error !== "object" || error === null || !("code" in error)) return null;
  return typeof error.code === "string" ? error.code : null;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

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
  const persistedTabRef = useRef<WorkspaceTab>(readStoredWorkspaceTab());
  const [activeTab, setActiveTab] = useState<WorkspaceTab | null>(null);
  const [pendingTab, setPendingTab] = useState<WorkspaceTab | null>(null);
  const [reconciling, setReconciling] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);
  const pendingSwitchRef = useRef<PendingWorkspaceSwitch | null>(null);
  const reconcilingRef = useRef(false);
  const switchGenerationRef = useRef(0);
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

  const clearPendingSwitch = useCallback(() => {
    pendingSwitchRef.current = null;
    reconcilingRef.current = false;
    setPendingTab(null);
    setReconciling(false);
  }, []);

  const commitWorkspaceTab = useCallback((tab: WorkspaceTab) => {
    if (persistedTabRef.current !== tab) {
      persistedTabRef.current = tab;
      persistWorkspaceTab(tab);
    }
    setActiveTab((currentTab) => (currentTab === tab ? currentTab : tab));
  }, []);

  useEffect(() => {
    if (!isMeetingRecording) return;
    switchGenerationRef.current += 1;
    clearPendingSwitch();
    setSwitchError(null);
    commitWorkspaceTab("meeting");
  }, [clearPendingSwitch, commitWorkspaceTab, isMeetingRecording]);

  const applyAuthoritativeSnapshot = useCallback((
    snapshot: RuntimeStateSnapshot,
    allowPendingTarget: boolean,
  ) => {
    const pending = pendingSwitchRef.current;
    if (!pending) {
      setActiveTab((currentTab) => (
        resolveWorkspaceTab(snapshot.mode, persistedTabRef.current, currentTab)
      ));
      return;
    }

    const isNewer = snapshot.runtime_revision > pending.startedRevision;
    const targetMatches = snapshot.mode === pending.tab;
    if (
      snapshot.mode !== "meeting"
      && !isNewer
      && !(allowPendingTarget && targetMatches && snapshot.runtime_revision >= pending.startedRevision)
    ) {
      return;
    }
    if (targetMatches && !allowPendingTarget && !reconcilingRef.current) {
      return;
    }

    clearPendingSwitch();
    setSwitchError(null);
    if (targetMatches) {
      commitWorkspaceTab(pending.tab);
    } else {
      setActiveTab((currentTab) => (
        resolveWorkspaceTab(snapshot.mode, persistedTabRef.current, currentTab)
      ));
    }
  }, [clearPendingSwitch, commitWorkspaceTab]);

  useEffect(() => {
    if (!commandSocket.snapshot) return;
    applyAuthoritativeSnapshot(commandSocket.snapshot, false);
  }, [applyAuthoritativeSnapshot, commandSocket.snapshot, reconciling]);

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

  const handleTabChange = useCallback(
    (newTab: WorkspaceTab) => {
      if (!commandSocket.snapshot) {
        return;
      }
      if (newTab === "meeting") {
        commitWorkspaceTab("meeting");
        setSwitchError(null);
        return;
      }

      if (pendingSwitchRef.current || activeTab === newTab) return;

      const startedRevision = commandSocket.highestRuntimeRevision
        ?? commandSocket.snapshot.runtime_revision;
      const generation = switchGenerationRef.current + 1;
      switchGenerationRef.current = generation;
      const pendingSwitch: PendingWorkspaceSwitch = {
        tab: newTab,
        startedRevision,
        generation,
      };
      pendingSwitchRef.current = pendingSwitch;
      reconcilingRef.current = false;
      setPendingTab(newTab);
      setReconciling(false);
      setSwitchError(null);

      const command = newTab === "subtitles"
        ? { cmd: "start_subtitles" as const }
        : { cmd: "start_assistant" as const };
      void commandSocket.sendCommand(command).then(
        (snapshot) => {
          if (pendingSwitchRef.current?.generation !== generation) return;
          applyAuthoritativeSnapshot(snapshot, true);
        },
        (error: unknown) => {
          if (pendingSwitchRef.current?.generation !== generation) return;
          if (errorCode(error) === "timeout") {
            reconcilingRef.current = true;
            setReconciling(true);
            void commandSocket.reconcileRuntime().then(
              (snapshot) => {
                if (pendingSwitchRef.current?.generation !== generation) return;
                applyAuthoritativeSnapshot(snapshot, true);
              },
              (reconcileError: unknown) => {
                if (pendingSwitchRef.current?.generation !== generation) return;
                const message = errorMessage(reconcileError, "运行时状态对账失败");
                setSwitchError(message);
                showToast(message, "error");
              },
            );
            return;
          }

          const message = errorMessage(error, "工作区切换失败");
          clearPendingSwitch();
          setSwitchError(message);
          showToast(message, "error");
        },
      );
    },
    [
      activeTab,
      applyAuthoritativeSnapshot,
      clearPendingSwitch,
      commandSocket,
      commitWorkspaceTab,
    ],
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
    <div className="app-container" data-workspace={activeTab || "assistant"}>
      <StatusBar
        commandSocket={commandSocket}
        onOpenShortcuts={() => setShortcutsOpen(true)}
        activeTab={activeTab}
        pendingTab={pendingTab}
        reconciling={reconciling}
        switchError={switchError}
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
