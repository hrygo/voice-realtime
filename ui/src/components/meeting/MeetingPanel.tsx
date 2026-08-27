import { useEffect, useState, useCallback } from "react";
import { useMeetingStore } from "../../stores/meetingStore";
import { useUISettingsStore } from "../../stores/uiSettingsStore";
import { useInnerOSStore, InnerOSUnsavedTray } from "../../features/innerOS";
import type { CommandSocketApi } from "../../hooks/useCommandSocket";
import { MeetingHistorySidebar } from "./MeetingHistorySidebar";
import { MeetingIdleView } from "./MeetingIdleView";
import { formatElapsed, MeetingRecordingView } from "./MeetingRecordingView";
import { MeetingFinalizingView } from "./MeetingFinalizingView";
import { MeetingDetailView } from "./MeetingDetailView";
import { MeetingSpeakerModal } from "./MeetingSpeakerModal";
import { MeetingDeleteModal } from "./MeetingDeleteModal";
import { showToast } from "../Toast";
import type { MeetingDetail } from "../../contracts/meetingContract";
import "./MeetingPanel.css";

interface MeetingPanelProps {
  commandSocket: CommandSocketApi;
}

export default function MeetingPanel({ commandSocket }: MeetingPanelProps) {
  const store = useMeetingStore();
  const [isStarting, setIsStarting] = useState(false);
  const [isEnding, setIsEnding] = useState(false);

  const isMeetingActive = store.status === "recording" || store.status === "finalizing";

  // Collapsible sidebar state & persistence
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState<boolean>(() => {
    try {
      if (typeof window !== "undefined" && window.localStorage) {
        return window.localStorage.getItem("voice-studio:meeting-sidebar-collapsed") === "true";
      }
    } catch {
      // Ignore
    }
    return false;
  });

  const toggleSidebarCollapse = useCallback(() => {
    setIsSidebarCollapsed((prev) => {
      const next = !prev;
      try {
        if (typeof window !== "undefined" && window.localStorage) {
          window.localStorage.setItem("voice-studio:meeting-sidebar-collapsed", String(next));
        }
      } catch {
        // Ignore
      }
      return next;
    });
  }, []);

  // Global ⌘+B shortcut for sidebar collapse (works everywhere)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const isCmdOrCtrl = e.metaKey || e.ctrlKey;
      const isB = e.key.toLowerCase() === "b" || e.code === "KeyB";
      if (isCmdOrCtrl && !e.altKey && !e.shiftKey && isB) {
        e.preventDefault();
        toggleSidebarCollapse();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleSidebarCollapse]);

  // Inner OS unsaved tray state
  const unsavedExchanges = useInnerOSStore((s) => s.unsavedExchanges);
  const saveExchangeAction = useInnerOSStore((s) => s.saveExchangeAction);
  const dismissUnsavedItem = useInnerOSStore((s) => s.dismissUnsavedItem);

  // Speaker modal
  const [speakerModalOpen, setSpeakerModalOpen] = useState(false);
  const [speakerModalTarget, setSpeakerModalTarget] = useState<{
    key: string;
    currentName: string;
  } | null>(null);

  // Delete modal for active/selected
  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [deleteTargetId, setDeleteTargetId] = useState<string | null>(null);
  const [deleteTargetTitle, setDeleteTargetTitle] = useState("");

  // Live timer for active recording in background
  const [liveElapsed, setLiveElapsed] = useState(0);
  useEffect(() => {
    if (!isMeetingActive || !store.sessionStartedAt) {
      setLiveElapsed(0);
      return;
    }
    const startMs = Date.parse(store.sessionStartedAt);
    const tick = () => {
      const diff = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
      setLiveElapsed(diff);
    };
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [isMeetingActive, store.sessionStartedAt]);

  const handleNewMeeting = () => {
    store.resetActiveSession();
  };

  // Global Esc shortcut to return to active recording / new meeting view
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) {
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        if (isMeetingActive) {
          store.returnToActiveMeeting();
        } else {
          handleNewMeeting();
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isMeetingActive, store.returnToActiveMeeting, handleNewMeeting]);

  // Load history on mount
  useEffect(() => {
    void store.fetchHistory();
  }, []);

  const handleStartMeeting = async (title: string, maxSpeakers?: number) => {
    if (!commandSocket.ready) {
      showToast("控制端连接中，请稍候再试", "warning");
      return;
    }

    setIsStarting(true);
    try {
      const resp = await commandSocket.sendCommand({
        cmd: "start_meeting",
        title,
        max_speakers: maxSpeakers,
        contract_version: "1",
      });
      if (resp.active_meeting_id) {
        useMeetingStore.setState({
          activeMeetingId: resp.active_meeting_id,
          activeMeeting: {
            id: resp.active_meeting_id,
            title,
            status: "recording",
            language: "Chinese",
            audio_source: "microphone",
            started_at: new Date().toISOString(),
            ended_at: null,
            transcript_revision: 0,
            content_revision: 0,
            interruption_reason: null,
            speakers: {},
            latest_minutes: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
          status: "recording",
          selectedMeetingId: null,
          selectedMeeting: null,
          sessionStartedAt: new Date().toISOString(),
          sessionEndedAt: null,
          segments: [],
          gaps: [],
          minutes: null,
          minutesHistory: [],
        });
        void store.fetchHistory();
      }
      showToast("已成功开启会议模式", "success");
      store.returnToActiveMeeting();
    } catch (err) {
      showToast(err instanceof Error ? err.message : "开始会议失败", "error");
    } finally {
      setIsStarting(false);
    }
  };

  const handleEndMeeting = async () => {
    const meetingId =
      store.activeMeetingId || useUISettingsStore.getState().activeMeetingId;
    if (!commandSocket.ready) {
      showToast("控制端连接中，请稍候再试", "warning");
      return;
    }

    setIsEnding(true);
    try {
      if (meetingId) {
        await commandSocket.sendCommand({
          cmd: "end_meeting",
          meeting_id: meetingId,
          contract_version: "1",
        });
      } else {
        await commandSocket.sendCommand({
          cmd: "stop_active_mode",
          contract_version: "1",
        });
      }
      void store.fetchHistory();
      showToast("会议已结束，正在冲刷转录并排队生成 AI 纪要", "info");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "结束会议失败", "error");
    } finally {
      setIsEnding(false);
    }
  };

  const handleRenameSpeaker = (speakerKey: string, currentName: string) => {
    setSpeakerModalTarget({ key: speakerKey, currentName });
    setSpeakerModalOpen(true);
  };

  const handleSaveSpeakerName = async (speakerKey: string, newName: string) => {
    if (store.selectedMeetingId) {
      await store.updateSpeakerName(store.selectedMeetingId, speakerKey, newName);
    } else if (store.activeMeetingId) {
      store.setSpeaker(speakerKey, newName, store.contentRevision + 1);
      await store.updateSpeakerName(store.activeMeetingId, speakerKey, newName);
    }
    showToast(`说话人已重命名为 “${newName}”`, "success");
  };

  const handleOpenDelete = (id: string, title: string) => {
    setDeleteTargetId(id);
    setDeleteTargetTitle(title);
    setDeleteModalOpen(true);
  };

  const handleConfirmDelete = async () => {
    if (!deleteTargetId) return;
    try {
      await store.deleteMeeting(deleteTargetId);
      showToast("会议记录已永久删除", "success");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "删除会议失败", "error");
    }
  };

  // Construct active meeting detail object if in completed/interrupted mode without selected history
  const activeMeetingDetail: MeetingDetail | null =
    store.activeMeetingId && (store.status === "completed" || store.status === "interrupted" || store.status === "storage_error")
      ? {
          id: store.activeMeetingId,
          title: store.activeMeeting?.title || "当前会议",
          status: store.status,
          language: "Chinese",
          audio_source: "microphone",
          started_at: store.sessionStartedAt,
          ended_at: store.sessionEndedAt,
          transcript_revision: store.transcriptRevision,
          content_revision: store.contentRevision,
          interruption_reason: store.interruptionReason,
          speakers: store.speakers,
          latest_minutes: store.minutes,
          created_at: store.sessionStartedAt || new Date().toISOString(),
          updated_at: store.sessionEndedAt || new Date().toISOString(),
        }
      : null;

  return (
    <div className="meeting-workspace">
      {/* 历史会议侧边栏 */}
      <MeetingHistorySidebar
        historyList={store.historyList}
        selectedMeetingId={store.selectedMeetingId}
        activeMeetingId={store.activeMeetingId}
        activeMeetingTitle={store.activeMeeting?.title || "当前会议"}
        activeStatus={store.status}
        activeStartedAt={store.sessionStartedAt}
        activeSegmentsCount={store.segments.length}
        micMuted={store.health.mic_muted}
        nextCursor={store.nextCursor}
        isLoading={store.isLoadingHistory}
        isCollapsed={isSidebarCollapsed}
        onToggleCollapse={toggleSidebarCollapse}
        onSelectMeeting={(id) => void store.selectMeeting(id)}
        onReturnToActive={isMeetingActive ? () => store.returnToActiveMeeting() : handleNewMeeting}
        onNewMeeting={handleNewMeeting}
        onRefresh={() => void store.fetchHistory()}
        onLoadMore={() => void store.fetchHistory(store.nextCursor)}
        onDeleteMeeting={(id) => {
          const item = store.historyList.find((m) => m.id === id);
          handleOpenDelete(id, item?.title || "会议");
          return Promise.resolve();
        }}
      />

      {/* 主工作区内容 */}
      <main className="meeting-main-content">
        {/* 1. 如果在浏览历史会议，且后台有正在录制/封存的会议，显示常驻悬浮控制条 */}
        {store.selectedMeetingId && store.selectedMeeting && isMeetingActive && (
          <div className="meeting-live-banner">
            <div className="live-banner-info">
              <span className="live-banner-pulse-dot" />
              <span className="live-banner-tag">
                {store.status === "recording" ? "后台录制中" : "后台封存中"}
              </span>
              <span className="live-banner-title" title={store.activeMeeting?.title || "当前会议"}>
                {store.activeMeeting?.title || "当前会议"}
              </span>
              <span className="live-banner-timer">⏱️ {formatElapsed(liveElapsed)}</span>
              <span className="live-banner-segments">🎙️ {store.segments.length} 段已记录</span>
            </div>

            <div className="live-banner-actions">
              <button
                type="button"
                className={`btn-live-banner-mic ${store.health.mic_muted ? "muted" : ""}`}
                onClick={() => {
                  void commandSocket.sendCommand({
                    cmd: "set_mic_muted",
                    muted: !store.health.mic_muted,
                  });
                }}
                title={store.health.mic_muted ? "点击解除麦克风静音" : "点击将麦克风静音"}
              >
                {store.health.mic_muted ? "🔇 解除静音" : "🎤 静音"}
              </button>
              <button
                type="button"
                className="btn-live-banner-end"
                onClick={handleEndMeeting}
                disabled={isEnding}
                title="结束当前正在录制的会议"
              >
                <span>⏹️</span>
                <span>{isEnding ? "封存中..." : "结束会议"}</span>
              </button>
              <button
                type="button"
                className="btn-live-banner-return"
                onClick={() => store.returnToActiveMeeting()}
                title="返回实时录制工作台 (快捷键 Esc)"
              >
                <span>返回实时工作台 ↗</span>
                <kbd className="banner-kbd">Esc</kbd>
              </button>
            </div>
          </div>
        )}

        {/* Unsaved Inner OS Tray for Finalizing or Completed Meetings */}
        {unsavedExchanges.length > 0 && (store.status === "finalizing" || store.status === "completed") && (
          <InnerOSUnsavedTray
            items={unsavedExchanges}
            onSaveItem={async (mId, qId) => {
              await saveExchangeAction(mId, qId);
            }}
            onDismissItem={dismissUnsavedItem}
          />
        )}

        {/* 视图分发 */}
        {store.selectedMeetingId && store.selectedMeeting ? (
          /* 1. 历史详情视图 */
          <MeetingDetailView
            meeting={store.selectedMeeting}
            segments={store.selectedSegments}
            minutes={store.selectedMinutes}
            minutesList={store.selectedMinutesList}
            selectedMinutesVersion={store.selectedMinutesVersion}
            onSelectMinutesVersion={(v) =>
              store.selectedMeetingId &&
              void store.selectHistoryMinutesVersion(store.selectedMeetingId, v)
            }
            onUpdateTitle={(title) =>
              store.selectedMeetingId
                ? store.updateMeetingTitle(store.selectedMeetingId, title)
                : Promise.resolve()
            }
            onGenerateTitle={() =>
              store.selectedMeetingId
                ? store.generateMeetingTitle(store.selectedMeetingId)
                : Promise.resolve()
            }
            onRenameSpeaker={handleRenameSpeaker}
            onRegenerateMinutes={() =>
              store.selectedMeetingId
                ? store.triggerGenerateMinutes(store.selectedMeetingId)
                : Promise.resolve()
            }
            onDeleteMeeting={() =>
              handleOpenDelete(
                store.selectedMeetingId!,
                store.selectedMeeting?.title || "会议",
              )
            }
            isMeetingActive={isMeetingActive}
            activeMeetingTitle={store.activeMeeting?.title}
            onReturnToActive={isMeetingActive ? store.returnToActiveMeeting : undefined}
            starredIds={
              store.selectedMeetingId
                ? store.starredMap[store.selectedMeetingId] || store.getStarredSegments(store.selectedMeetingId)
                : undefined
            }
            onToggleStarSegment={(segmentId) =>
              store.selectedMeetingId && store.toggleStarSegment(store.selectedMeetingId, segmentId)
            }
          />
        ) : store.status === "recording" ? (
          /* 2. 录制中视图 */
          <MeetingRecordingView
            startedAt={store.sessionStartedAt}
            segments={store.segments}
            partialText={store.partialText}
            partialSpeaker={store.partialSpeaker}
            gaps={store.gaps}
            micMuted={store.health.mic_muted}
            onToggleMic={() => {
              void commandSocket.sendCommand({
                cmd: "set_mic_muted",
                muted: !store.health.mic_muted,
              });
            }}
            onEndMeeting={handleEndMeeting}
            onRenameSpeaker={handleRenameSpeaker}
            isEnding={isEnding}
            isCalibrating={store.isCalibrating}
            starredIds={
              store.activeMeetingId
                ? store.starredMap[store.activeMeetingId] || store.getStarredSegments(store.activeMeetingId)
                : undefined
            }
            onToggleStarSegment={(segmentId) =>
              store.activeMeetingId && store.toggleStarSegment(store.activeMeetingId, segmentId)
            }
          />
        ) : store.status === "finalizing" ? (
          /* 3. 冲刷中视图 */
          <MeetingFinalizingView />
        ) : activeMeetingDetail ? (
          /* 4. 刚刚结束的活动会议详情 */
          <MeetingDetailView
            meeting={activeMeetingDetail}
            segments={store.segments}
            minutes={store.minutes}
            minutesList={store.minutesHistory}
            selectedMinutesVersion={store.activeMinutesVersion}
            onSelectMinutesVersion={(v) => store.setActiveMinutesVersion(v)}
            onUpdateTitle={(title) =>
              store.activeMeetingId
                ? store.updateMeetingTitle(store.activeMeetingId, title)
                : Promise.resolve()
            }
            onGenerateTitle={() =>
              store.activeMeetingId
                ? store.generateMeetingTitle(store.activeMeetingId)
                : Promise.resolve()
            }
            onRenameSpeaker={handleRenameSpeaker}
            onRegenerateMinutes={() =>
              store.activeMeetingId
                ? store.triggerGenerateMinutes(store.activeMeetingId)
                : Promise.resolve()
            }
            onDeleteMeeting={() =>
              handleOpenDelete(
                store.activeMeetingId!,
                activeMeetingDetail.title,
              )
            }
            isMeetingActive={false}
            onReturnToActive={undefined}
            starredIds={
              activeMeetingDetail.id
                ? store.starredMap[activeMeetingDetail.id] || store.getStarredSegments(activeMeetingDetail.id)
                : undefined
            }
            onToggleStarSegment={(segmentId) =>
              activeMeetingDetail.id && store.toggleStarSegment(activeMeetingDetail.id, segmentId)
            }
          />
        ) : (
          /* 5. 准备/闲置视图 */
          <MeetingIdleView
            health={store.health}
            onStartMeeting={handleStartMeeting}
            isStarting={isStarting}
          />
        )}
      </main>

      {/* 说话人重命名弹窗 */}
      {speakerModalTarget && (
        <MeetingSpeakerModal
          isOpen={speakerModalOpen}
          speakerKey={speakerModalTarget.key}
          currentDisplayName={speakerModalTarget.currentName}
          onClose={() => {
            setSpeakerModalOpen(false);
            setSpeakerModalTarget(null);
          }}
          onSave={handleSaveSpeakerName}
        />
      )}

      {/* 会议删除弹窗 */}
      {deleteModalOpen && deleteTargetId && (
        <MeetingDeleteModal
          isOpen={true}
          meetingTitle={deleteTargetTitle}
          onClose={() => setDeleteModalOpen(false)}
          onConfirm={handleConfirmDelete}
        />
      )}
    </div>
  );
}
