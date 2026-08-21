import { useEffect, useState } from "react";
import { useMeetingStore } from "../../stores/meetingStore";
import { useMeetingSocket } from "../../hooks/useMeetingSocket";
import type { CommandSocketApi } from "../../hooks/useCommandSocket";
import { MeetingHistorySidebar } from "./MeetingHistorySidebar";
import { MeetingIdleView } from "./MeetingIdleView";
import { MeetingRecordingView } from "./MeetingRecordingView";
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
  // Connect to /ws/v1/meetings
  useMeetingSocket();

  const store = useMeetingStore();
  const [isStarting, setIsStarting] = useState(false);
  const [isEnding, setIsEnding] = useState(false);

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

  // Load history on mount
  useEffect(() => {
    void store.fetchHistory();
  }, []);

  const handleStartMeeting = async (title: string) => {
    if (!commandSocket.ready) {
      showToast("控制端连接中，请稍候再试", "warning");
      return;
    }

    setIsStarting(true);
    try {
      await commandSocket.sendCommand({
        cmd: "start_meeting",
        title,
        contract_version: "1",
      });
      showToast("已成功开启会议模式", "success");
      // Deselect history to focus on live recording
      await store.selectMeeting(null);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "开始会议失败", "error");
    } finally {
      setIsStarting(false);
    }
  };

  const handleEndMeeting = async () => {
    if (!store.activeMeetingId) return;
    if (!commandSocket.ready) {
      showToast("控制端连接中，请稍候再试", "warning");
      return;
    }

    setIsEnding(true);
    try {
      await commandSocket.sendCommand({
        cmd: "end_meeting",
        meeting_id: store.activeMeetingId,
        contract_version: "1",
      });
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
      {/* 历史侧栏 */}
      <MeetingHistorySidebar
        historyList={store.historyList}
        selectedMeetingId={store.selectedMeetingId}
        activeMeetingId={store.activeMeetingId}
        nextCursor={store.nextCursor}
        isLoading={store.isLoadingHistory}
        onSelectMeeting={(id) => void store.selectMeeting(id)}
        onNewMeeting={() => void store.selectMeeting(null)}
        onLoadMore={() => void store.fetchHistory(store.nextCursor)}
        onDeleteMeeting={(id) => {
          const item = store.historyList.find((m) => m.id === id);
          handleOpenDelete(id, item?.title || "会议");
          return Promise.resolve();
        }}
      />

      {/* 主工作区内容 */}
      <main className="meeting-main-content">
        {/* 1. 如果选中了历史会议，展示历史详情视图 */}
        {store.selectedMeetingId && store.selectedMeeting ? (
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
