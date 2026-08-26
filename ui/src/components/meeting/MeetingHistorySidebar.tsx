import { useEffect, useState } from "react";
import type { MeetingStatus, MeetingSummary } from "../../contracts/meetingContract";
import { formatElapsed } from "./MeetingRecordingView";
import "../ModeSidebar.css";

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
  activeMeetingTitle?: string | null;
  activeStatus?: MeetingStatus | "idle";
  activeStartedAt?: string | null;
  activeSegmentsCount?: number;
  micMuted?: boolean;
  nextCursor: string | null;
  isLoading: boolean;
  onSelectMeeting: (id: string | null) => void;
  onReturnToActive: () => void;
  onNewMeeting: () => void;
  onRefresh?: () => void;
  onLoadMore: () => void;
  onDeleteMeeting: (id: string) => Promise<void>;
}

export function MeetingHistorySidebar({
  historyList,
  selectedMeetingId,
  activeMeetingId,
  activeMeetingTitle,
  activeStatus = "idle",
  activeStartedAt,
  activeSegmentsCount = 0,
  micMuted = false,
  nextCursor,
  isLoading,
  onSelectMeeting,
  onReturnToActive,
  onNewMeeting,
  onRefresh,
  onLoadMore,
  onDeleteMeeting,
}: MeetingHistorySidebarProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "recording" | "completed">("all");

  const isMeetingActive = activeStatus === "recording" || activeStatus === "finalizing";

  // Live timer for active recording
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!isMeetingActive || !activeStartedAt) {
      setElapsed(0);
      return;
    }
    const startMs = Date.parse(activeStartedAt);
    const update = () => {
      const diffSec = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
      setElapsed(diffSec);
    };
    update();
    const interval = setInterval(update, 1000);
    return () => clearInterval(interval);
  }, [isMeetingActive, activeStartedAt]);

  const filteredList = historyList.filter((m) => {
    if (statusFilter === "recording" && m.status !== "recording") return false;
    if (statusFilter === "completed" && m.status !== "completed") return false;
    return m.title.toLowerCase().includes(searchQuery.toLowerCase().trim());
  });

  return (
    <aside className="mode-sidebar meeting-sidebar" aria-label="会议导航">
      <div className="sidebar-header meeting-sidebar-header">
        <h2 className="sidebar-title meeting-sidebar-title">
          <span className="mode-sidebar-group-icon">📁</span>
          <span className="meeting-sidebar-title-copy">
            <strong>历史会议</strong>
            <small>会议记录与当前会话</small>
          </span>
        </h2>
        <div className="sidebar-header-actions">
          {onRefresh && (
            <button
              type="button"
              className="btn-refresh-history"
              onClick={onRefresh}
              disabled={isLoading}
              title="刷新历史会议列表"
            >
              <span className={isLoading ? "spin-icon" : ""}>🔄</span>
            </button>
          )}
          {isMeetingActive ? (
            selectedMeetingId ? (
              <button
                type="button"
                className="btn-new-meeting btn-return-active-header"
                onClick={onReturnToActive}
                title="返回正在进行的会议工作台"
              >
                <span className="btn-recording-pulse-dot" />
                <span>返回当前会议</span>
              </button>
            ) : (
              <div
                className="btn-new-meeting btn-recording-indicator"
                title="会议录制进行中"
              >
                <span className="btn-recording-pulse-dot" />
                <span>录制中</span>
              </div>
            )
          ) : (
            <button
              type="button"
              className="btn-new-meeting"
              onClick={onNewMeeting}
              title="发起新会议"
            >
              <span>+</span>
              <span>新会议</span>
            </button>
          )}
        </div>
      </div>

      <div className="mode-sidebar-scroll">
        {/* 置顶正在进行的会议专属动态卡片 */}
        {isMeetingActive && (
          <section className="mode-sidebar-group meeting-sidebar-group meeting-sidebar-group-status">
            <div className="mode-sidebar-group-header">
              <span className="mode-sidebar-group-title">
                <span className="mode-sidebar-group-icon">🎙️</span>
                当前会议
              </span>
              <span className="meeting-sidebar-status-label">
                {activeStatus === "recording" ? "录制中" : "封存中"}
              </span>
            </div>
            <div
              className={`pinned-active-meeting ${!selectedMeetingId ? "is-current-view" : "is-background"}`}
              onClick={onReturnToActive}
              title={selectedMeetingId ? "点击返回正在进行的实时会议工作台" : "当前正处于实时会议视图"}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onReturnToActive();
                }
              }}
            >
              <div className="pinned-active-badge-row">
                <span className="pinned-live-dot" />
                <span className="pinned-live-tag">
                  {activeStatus === "recording" ? "进行中" : "封存中"}
                </span>
                <span className="pinned-live-timer">{formatElapsed(elapsed)}</span>
                {micMuted && (
                  <span className="pinned-muted-badge" title="麦克风已静音">
                    🔇 静音
                  </span>
                )}
              </div>
              <div className="pinned-active-title">
                {activeMeetingTitle || "当前会议"}
              </div>
              <div className="pinned-active-footer">
                <span className="pinned-segments-count">{activeSegmentsCount} 个转录段落</span>
                <span className="pinned-return-hint">
                  {selectedMeetingId ? "返回实时 ↗" : "实时视图 ●"}
                </span>
              </div>
            </div>
          </section>
        )}

        <section className="mode-sidebar-group meeting-sidebar-group meeting-sidebar-group-controls">
          <div className="mode-sidebar-group-header">
            <span className="mode-sidebar-group-title">
              <span className="mode-sidebar-group-icon">⌕</span>
              查找会议
            </span>
          </div>

          <div className="history-search-wrap">
            <span className="history-search-icon">🔍</span>
            <input
              type="text"
              className="history-search-input"
              placeholder="搜索历史会议..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
            {searchQuery && (
              <button
                type="button"
                className="history-search-clear"
                onClick={() => setSearchQuery("")}
                title="清空搜索"
              >
                ✕
              </button>
            )}
          </div>

          <div className="meeting-filter-group" role="group" aria-label="会议状态筛选">
            <button
              type="button"
              className={`meeting-filter-button ${statusFilter === "all" ? "active" : ""}`}
              aria-pressed={statusFilter === "all"}
              onClick={() => setStatusFilter("all")}
            >
              全部
            </button>
            <button
              type="button"
              className={`meeting-filter-button ${statusFilter === "recording" ? "active recording" : ""}`}
              aria-pressed={statusFilter === "recording"}
              onClick={() => setStatusFilter("recording")}
            >
              录制中
            </button>
            <button
              type="button"
              className={`meeting-filter-button ${statusFilter === "completed" ? "active completed" : ""}`}
              aria-pressed={statusFilter === "completed"}
              onClick={() => setStatusFilter("completed")}
            >
              已完成
            </button>
          </div>
        </section>

        <section className="mode-sidebar-group meeting-sidebar-group meeting-sidebar-group-history">
          <div className="mode-sidebar-group-header">
            <span className="mode-sidebar-group-title">
              <span className="mode-sidebar-group-icon">☷</span>
              历史列表
            </span>
            <span className="mode-sidebar-group-meta">{filteredList.length}</span>
          </div>

          <div className="history-list">
            {filteredList.length === 0 && !isLoading && !isMeetingActive && (
              <div className="history-empty">
                {searchQuery ? `未找到包含 "${searchQuery}" 的会议` : "暂无会议记录"}
              </div>
            )}

            {filteredList.map((m) => {
              const isSelected =
                selectedMeetingId === m.id ||
                (!selectedMeetingId && activeMeetingId === m.id && !isMeetingActive);
              const isActiveRecording = activeMeetingId === m.id && isMeetingActive;
              const statusInfo = getStatusLabel(m.status);

              return (
                <div
                  key={m.id}
                  className={`history-item ${isSelected ? "active" : ""} ${isActiveRecording ? "is-live-item" : ""}`}
                  onClick={() => {
                    if (isActiveRecording) {
                      onReturnToActive();
                    } else {
                      onSelectMeeting(m.id);
                    }
                  }}
                  title={isActiveRecording ? "点击返回正在录制的实时工作台" : undefined}
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
                    {!isActiveRecording && m.status !== "recording" && (
                      <button
                        type="button"
                        className="status-icon-btn history-delete-btn"
                        title="删除此会议"
                        onClick={(e) => {
                          e.stopPropagation();
                          void onDeleteMeeting(m.id);
                        }}
                      >
                        🗑️
                      </button>
                    )}
                    {isActiveRecording && (
                      <span className="active-item-hint">点击返回 ↗</span>
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
        </section>
      </div>

    </aside>
  );
}
