import { useEffect, useState } from "react";
import type { MeetingStatus, MeetingSummary } from "../../contracts/meetingContract";
import { formatElapsed } from "./MeetingRecordingView";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  FolderIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  PlusIcon,
  RefreshCwIcon,
  SearchIcon,
} from "../Icons";
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

export interface MeetingHistorySidebarProps {
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
  historyError?: string | null;
  isCollapsed?: boolean;
  onToggleCollapse?: () => void;
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
  historyError = null,
  isCollapsed = false,
  onToggleCollapse,
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
    <aside
      className={`mode-sidebar meeting-sidebar ${isCollapsed ? "is-collapsed" : ""}`}
      aria-label="会议导航"
      aria-expanded={!isCollapsed}
    >
      {/* 边缘快速折叠/展开把手 (Notion / Linear 风格交互) */}
      {onToggleCollapse && (
        <div
          className="sidebar-edge-toggle-handle"
          onClick={onToggleCollapse}
          title={isCollapsed ? "展开边栏 (⌘+B)" : "收起边栏 (⌘+B)"}
          role="button"
          tabIndex={-1}
        >
          <div className="edge-handle-pill" aria-hidden="true">
            {isCollapsed ? <ChevronRightIcon size={10} /> : <ChevronLeftIcon size={10} />}
          </div>
        </div>
      )}

      {/* 1. 折叠极简导轨形态 (52px Icon Rail) */}
      <div className="meeting-sidebar-collapsed-strip" aria-hidden={!isCollapsed}>
        {/* 展开按钮 (带 Tooltip) */}
        {onToggleCollapse && (
          <div className="collapsed-action-btn-wrapper">
            <button
              type="button"
              className="meeting-sidebar-toggle-btn btn-expand-sidebar"
              onClick={onToggleCollapse}
              aria-label="展开历史会议边栏"
            >
              <PanelLeftOpenIcon size={16} />
            </button>
            <div className="collapsed-item-flyout action-flyout" role="tooltip">
              <span>展开边栏</span>
              <kbd className="flyout-kbd">⌘B</kbd>
            </div>
          </div>
        )}

        {/* 正在录制中的 Mini Wave 拟真声学跳动胶囊 */}
        {isMeetingActive && (
          <>
            <div
              className="meeting-sidebar-collapsed-pulse is-recording"
              onClick={onReturnToActive}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onReturnToActive();
                }
              }}
            >
              <div className="collapsed-pulse-top">
                <span className="pinned-live-dot" />
                <span className="collapsed-mini-wave" aria-hidden="true">
                  <span className="mini-wave-bar bar-1" />
                  <span className="mini-wave-bar bar-2" />
                  <span className="mini-wave-bar bar-3" />
                </span>
              </div>
              <span className="collapsed-timer">{formatElapsed(elapsed)}</span>

              {/* Hover Flyout Preview Card */}
              <div className="collapsed-item-flyout" role="tooltip">
                <div className="flyout-title">{activeMeetingTitle || "当前会议"}</div>
                <div className="flyout-meta">
                  <span className="status-badge-chip-sm recording">
                    {activeStatus === "recording" ? "录制中" : "封存中"}
                  </span>
                  <span>{formatElapsed(elapsed)}</span>
                  {micMuted && <span>(🔇 静音)</span>}
                </div>
              </div>
            </div>
            <div className="collapsed-rail-divider" />
          </>
        )}

        {/* 快速新建会议 */}
        <div className="collapsed-action-btn-wrapper">
          <button
            type="button"
            className="meeting-sidebar-toggle-btn"
            onClick={onNewMeeting}
            aria-label="发起新会议"
          >
            <PlusIcon size={16} />
          </button>
          <div className="collapsed-item-flyout action-flyout" role="tooltip">
            <span>发起新会议</span>
          </div>
        </div>

        {/* 快速搜索 (点击展开并自动聚焦输入框) */}
        <div className="collapsed-action-btn-wrapper">
          <button
            type="button"
            className="meeting-sidebar-toggle-btn"
            onClick={() => {
              onToggleCollapse?.();
              setTimeout(() => {
                const searchInput = document.querySelector(".history-search-input") as HTMLInputElement | null;
                searchInput?.focus();
              }, 120);
            }}
            aria-label="搜索历史会议"
          >
            <SearchIcon size={15} />
          </button>
          <div className="collapsed-item-flyout action-flyout" role="tooltip">
            <span>搜索会议</span>
            <kbd className="flyout-kbd">/</kbd>
          </div>
        </div>

        <div className="collapsed-rail-divider" />

        {/* 最近会议快速导轨与悬浮预览卡片 */}
        {historyList.length > 0 && (
          <div className="collapsed-recent-rail" role="list" aria-label="最近会议预览">
            {historyList.slice(0, 4).map((m) => {
              const isSelected = selectedMeetingId === m.id;
              const isLive = activeMeetingId === m.id && isMeetingActive;
              const statusInfo = getStatusLabel(m.status);
              const initialChar = m.title ? m.title.trim().charAt(0).toUpperCase() : "会";

              return (
                <div
                  key={m.id}
                  className={`collapsed-recent-item ${isSelected ? "active" : ""} ${isLive ? "is-live" : ""}`}
                  onClick={() => {
                    if (isLive) {
                      onReturnToActive();
                    } else {
                      onSelectMeeting(m.id);
                    }
                  }}
                  role="listitem"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      if (isLive) onReturnToActive();
                      else onSelectMeeting(m.id);
                    }
                  }}
                >
                  <span className="collapsed-item-avatar">{initialChar}</span>
                  <span className={`collapsed-item-dot ${statusInfo.className}`} />

                  {/* Floating Preview Card on Hover */}
                  <div className="collapsed-item-flyout" role="tooltip">
                    <div className="flyout-title">{m.title}</div>
                    <div className="flyout-meta">
                      <span className={`status-badge-chip-sm ${statusInfo.className}`}>{statusInfo.text}</span>
                      <span>{formatMeetingDate(m.started_at || m.created_at)}</span>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* 底部快捷键指示 */}
        <div className="collapsed-rail-footer" title="快捷键 ⌘+B 展开/收起边栏">
          <kbd className="sidebar-rail-kbd">⌘B</kbd>
        </div>
      </div>

      {/* 2. 展开全量内容形态 (Full Sidebar Content) */}
      <div className="meeting-sidebar-full-content" aria-hidden={isCollapsed}>
        <div className="sidebar-header meeting-sidebar-header">
          <h2 className="sidebar-title meeting-sidebar-title">
            <span className="sidebar-header-icon">
              <FolderIcon size={16} />
            </span>
            <span className="meeting-sidebar-title-copy">
              <strong>历史会议</strong>
              <small>会议记录与当前会话</small>
            </span>
          </h2>
          <div className="sidebar-header-actions">
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
                <PlusIcon size={13} />
                <span>新会议</span>
              </button>
            )}
            {onRefresh && (
              <button
                type="button"
                className="btn-refresh-history"
                onClick={onRefresh}
                disabled={isLoading}
                title="刷新历史会议列表"
                aria-label="刷新历史会议列表"
              >
                <span className={isLoading ? "spin-icon" : ""}>
                  <RefreshCwIcon size={13} />
                </span>
              </button>
            )}
            {onToggleCollapse && (
              <button
                type="button"
                className="btn-refresh-history btn-collapse-sidebar"
                onClick={onToggleCollapse}
                title="收起历史边栏 (⌘+B)"
                aria-label="收起历史边栏"
              >
                <PanelLeftCloseIcon size={15} />
              </button>
            )}
          </div>
        </div>

        <div className="mode-sidebar-scroll">
          {/* 置顶正在进行的会议专属动态卡片 */}
          {isMeetingActive && (
            <div className="pinned-active-container meeting-sidebar-group-status">
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
            </div>
          )}

          {/* 搜索与状态筛选控制栏 */}
          <div className="history-search-and-filter meeting-sidebar-group-controls">
            <div className="history-search-wrap">
              <span className="history-search-icon">
                <SearchIcon size={13} />
              </span>
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
          </div>

          {/* 历史会议列表 */}
          <div className="history-list-section meeting-sidebar-group-history">
            <div className="history-list-section-header">
              <span className="history-list-section-title">
                会议记录
              </span>
              <span className="history-list-section-count">{filteredList.length}</span>
            </div>

            <div className="history-list">
              {historyError && (
                <div className="history-empty history-load-error" role="alert">
                  <div>加载失败：{historyError}</div>
                  {onRefresh && (
                    <button
                      type="button"
                      className="btn-load-more"
                      onClick={onRefresh}
                      disabled={isLoading}
                    >
                      重试
                    </button>
                  )}
                </div>
              )}

              {!historyError && filteredList.length === 0 && !isLoading && !isMeetingActive && (
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
          </div>
        </div>
      </div>
    </aside>
  );
}
