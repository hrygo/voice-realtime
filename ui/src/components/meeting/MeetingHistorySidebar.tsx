import { useState } from "react";
import type { MeetingStatus, MeetingSummary } from "../../contracts/meetingContract";
import { MeetingDeleteModal } from "./MeetingDeleteModal";

export function formatMeetingDate(dateStr: string | null): string {
  if (!dateStr) return "未知时间";
  try {
    const d = new Date(dateStr);
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const h = String(d.getHours()).padStart(2, "0");
    const min = String(d.getMinutes()).padStart(2, "0");
    return `${m}/${day} ${h}:${min}`;
  } catch {
    return dateStr;
  }
}

export function getStatusLabel(status: MeetingStatus): { text: string; className: string } {
  switch (status) {
    case "recording":
      return { text: "录制中", className: "recording" };
    case "finalizing":
      return { text: "封存中", className: "finalizing" };
    case "completed":
      return { text: "已完成", className: "completed" };
    case "interrupted":
      return { text: "已中断", className: "interrupted" };
    case "storage_error":
      return { text: "存储异常", className: "storage_error" };
    default:
      return { text: status, className: "default" };
  }
}

interface MeetingHistorySidebarProps {
  historyList: readonly MeetingSummary[];
  selectedMeetingId: string | null;
  activeMeetingId: string | null;
  nextCursor: string | null;
  isLoading: boolean;
  onSelectMeeting: (id: string | null) => void;
  onNewMeeting: () => void;
  onLoadMore: () => void;
  onDeleteMeeting: (id: string) => Promise<void>;
}

export function MeetingHistorySidebar({
  historyList,
  selectedMeetingId,
  activeMeetingId,
  nextCursor,
  isLoading,
  onSelectMeeting,
  onNewMeeting,
  onLoadMore,
  onDeleteMeeting,
}: MeetingHistorySidebarProps) {
  const [deleteTarget, setDeleteTarget] = useState<MeetingSummary | null>(null);

  return (
    <aside className="meeting-sidebar">
      <div className="sidebar-header">
        <h2 className="sidebar-title">
          <span>📁</span>
          <span>历史会议</span>
        </h2>
        <button
          type="button"
          className="btn-new-meeting"
          onClick={onNewMeeting}
          title="发起新会议"
        >
          <span>+</span>
          <span>新会议</span>
        </button>
      </div>

      <div className="history-list">
        {historyList.length === 0 && !isLoading && (
          <div className="history-empty">暂无会议记录</div>
        )}

        {historyList.map((m) => {
          const isSelected = selectedMeetingId === m.id;
          const isActiveRecording = activeMeetingId === m.id;
          const statusInfo = getStatusLabel(m.status);

          return (
            <div
              key={m.id}
              className={`history-item ${isSelected ? "active" : ""}`}
              onClick={() => onSelectMeeting(m.id)}
            >
              <div className="history-item-top">
                <span className="history-item-title" title={m.title}>
                  {isActiveRecording ? "🔴 " : ""}{m.title}
                </span>
                <span className={`status-badge ${statusInfo.className}`}>
                  {statusInfo.text}
                </span>
              </div>

              <div className="history-item-meta">
                <span>{formatMeetingDate(m.started_at || m.created_at)}</span>
                {m.status !== "recording" && (
                  <button
                    type="button"
                    className="status-icon-btn"
                    style={{
                      fontSize: "0.72rem",
                      padding: "2px",
                      opacity: isSelected ? 0.9 : 0.4,
                    }}
                    title="删除此会议"
                    onClick={(e) => {
                      e.stopPropagation();
                      setDeleteTarget(m);
                    }}
                  >
                    🗑️
                  </button>
                )}
              </div>
            </div>
          );
        })}

        {nextCursor && (
          <button
            type="button"
            className="btn-load-more"
            onClick={onLoadMore}
            disabled={isLoading}
          >
            {isLoading ? "加载中..." : "加载更多历史"}
          </button>
        )}
      </div>

      {deleteTarget && (
        <MeetingDeleteModal
          isOpen={true}
          meetingTitle={deleteTarget.title}
          onClose={() => setDeleteTarget(null)}
          onConfirm={async () => {
            await onDeleteMeeting(deleteTarget.id);
            setDeleteTarget(null);
          }}
        />
      )}
    </aside>
  );
}
