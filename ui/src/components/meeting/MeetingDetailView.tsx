import { useState, useEffect, useRef, type CSSProperties } from "react";
import type {
  ExportFormat,
  MeetingDetail,
  MeetingMinutesVersion,
  TranscriptSegment,
} from "../../contracts/meetingContract";
import { MeetingTranscriptViewer } from "./MeetingTranscriptViewer";
import { MeetingMinutesViewer } from "./MeetingMinutesViewer";
import { InnerOSHistoryTab } from "../../features/innerOS";
import { exportMeetingData } from "../../utils/exportUtils";
import { copyTextToClipboard } from "../../utils/clipboard";
import { showToast } from "../Toast";
import { ChevronLeftIcon } from "../Icons";

interface MeetingDetailViewProps {
  meeting: MeetingDetail;
  segments: readonly TranscriptSegment[];
  minutes: MeetingMinutesVersion | null;
  minutesList: readonly MeetingMinutesVersion[];
  selectedMinutesVersion: number | null;
  onSelectMinutesVersion: (version: number) => void;
  onUpdateTitle: (title: string) => Promise<void>;
  onGenerateTitle: () => Promise<void>;
  onRenameSpeaker: (speakerKey: string, currentName: string) => void;
  onRegenerateMinutes: () => Promise<void>;
  onDeleteMeeting: () => Promise<void> | void;
  isMeetingActive?: boolean;
  activeMeetingTitle?: string | null;
  onReturnToActive?: () => void;
  starredIds?: ReadonlySet<string>;
  onToggleStarSegment?: (segmentId: string) => void;
}

export function MeetingDetailView({
  meeting,
  segments,
  minutes,
  minutesList,
  selectedMinutesVersion,
  onSelectMinutesVersion,
  onUpdateTitle,
  onGenerateTitle,
  onRenameSpeaker,
  onRegenerateMinutes,
  onDeleteMeeting,
  isMeetingActive = false,
  activeMeetingTitle,
  onReturnToActive,
  starredIds,
  onToggleStarSegment,
}: MeetingDetailViewProps) {
  const [title, setTitle] = useState(meeting.title);
  const [isGeneratingTitle, setIsGeneratingTitle] = useState(false);
  const [isExportMenuOpen, setIsExportMenuOpen] = useState(false);
  const [highlightedSegmentId, setHighlightedSegmentId] = useState<string | null>(null);
  const [isRegenerating, setIsRegenerating] = useState(false);
  const [activeRightTab, setActiveRightTab] = useState<"minutes" | "inner_os">("minutes");

  const handleGenerateTitle = async () => {
    if (isGeneratingTitle) return;
    if (segments.length === 0) {
      showToast("当前会议暂无转录内容，无法由 AI 提炼标题", "warning");
      return;
    }
    setIsGeneratingTitle(true);
    try {
      await onGenerateTitle();
      showToast("✨ AI 已成功提炼并更新会议标题", "success");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "AI 标题生成失败", "error");
    } finally {
      setIsGeneratingTitle(false);
    }
  };

  const handleSaveTitle = async () => {
    if (!title.trim() || title === meeting.title) {
      setTitle(meeting.title);
      return;
    }
    try {
      await onUpdateTitle(title.trim());
      showToast("会议标题已更新", "success");
    } catch {
      showToast("更新标题失败", "error");
    }
  };

  const handleExport = async (format: ExportFormat) => {
    try {
      exportMeetingData(meeting, segments, minutes, format, starredIds);
      setIsExportMenuOpen(false);
      showToast(`已成功下载 .${format} 文件`, "success");
    } catch {
      showToast("导出失败", "error");
    }
  };

  const handleCopyReport = async () => {
    if (!minutes?.content_json) {
      showToast("当前暂无结构化纪要可供复制", "warning");
      return;
    }
    const c = minutes.content_json;
    let text = `# 会议总结与汇报：${meeting.title}\n\n`;
    text += `## 会议概述\n${c.overview || "无"}\n\n`;
    if (c.topics && c.topics.length > 0) {
      text += `## 核心议题\n`;
      c.topics.forEach((t, i) => {
        text += `${i + 1}. **${t.title}**\n   ${t.summary}\n`;
      });
      text += "\n";
    }
    if (c.decisions && c.decisions.length > 0) {
      text += `## 决策事项\n`;
      c.decisions.forEach((d, i) => {
        text += `${i + 1}. **${d.content}**\n`;
      });
      text += "\n";
    }
    if (c.action_items && c.action_items.length > 0) {
      text += `## 待办行动项\n`;
      c.action_items.forEach((a) => {
        const owner = a.owner ? `@${a.owner}` : "未指定负责人";
        const dueDate = a.due_date ? ` (截止: ${a.due_date})` : "";
        text += `- [ ] ${a.task} [${owner}${dueDate}]\n`;
      });
    }
    await copyTextToClipboard(text);
    setIsExportMenuOpen(false);
    showToast("已复制会议汇报格式到剪贴板 📋", "success");
  };

  const handleCopyChecklist = async () => {
    if (!minutes?.content_json?.action_items || minutes.content_json.action_items.length === 0) {
      showToast("当前纪要中暂无待办事项", "warning");
      return;
    }
    let text = `### 待办任务清单 (${meeting.title})\n\n`;
    minutes.content_json.action_items.forEach((a) => {
      const owner = a.owner ? ` (@${a.owner})` : "";
      const dueDate = a.due_date ? ` [截止: ${a.due_date}]` : "";
      text += `- [ ] ${a.task}${owner}${dueDate}\n`;
    });
    await copyTextToClipboard(text);
    setIsExportMenuOpen(false);
    showToast("已复制待办清单 (Checklist) 📋", "success");
  };

  const handleCopyStarred = async () => {
    if (!starredIds || starredIds.size === 0) {
      showToast("当前会议暂无重点发言", "warning");
      return;
    }
    const starredSegments = segments.filter((s) => starredIds.has(s.id));
    let text = `### 重点发言摘录 (${meeting.title})\n\n`;
    starredSegments.forEach((s, idx) => {
      text += `${idx + 1}. **${s.speaker_name}**: ${s.text}\n`;
    });
    await copyTextToClipboard(text);
    setIsExportMenuOpen(false);
    showToast(`已复制 ${starredSegments.length} 段重点发言 📋`, "success");
  };

  const handleRegenerate = async () => {
    if (isRegenerating) return;
    setIsRegenerating(true);
    try {
      await onRegenerateMinutes();
      showToast("已触发纪要重新生成", "info");
    } catch {
      showToast("触发重新生成失败", "error");
    } finally {
      setIsRegenerating(false);
    }
  };

  const handleEvidenceClick = (segmentId: string) => {
    setHighlightedSegmentId(segmentId);
    setTimeout(() => {
      const el = document.getElementById(`segment-${segmentId}`);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.remove("is-evidence-target-inneros");
        void el.offsetWidth;
        el.classList.add("is-evidence-target-inneros");
        setTimeout(() => {
          el.classList.remove("is-evidence-target-inneros");
        }, 3000);
      }
    }, 50);
  };

  // Keyboard navigation
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (isMeetingActive && onReturnToActive) {
          e.preventDefault();
          onReturnToActive();
          return;
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isMeetingActive, onReturnToActive]);

  // Dual Pane Split Dragging
  const [splitPercent, setSplitPercent] = useState<number>(() => {
    try {
      if (typeof window !== "undefined" && window.localStorage) {
        const saved = window.localStorage.getItem("sona:meeting-split-percent");
        if (saved) {
          const val = parseFloat(saved);
          if (!isNaN(val) && val >= 25 && val <= 75) return val;
        }
      }
    } catch {
      // Ignore localStorage access restrictions
    }
    return 48;
  });
  const [isDraggingSplitter, setIsDraggingSplitter] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleSplitterMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsDraggingSplitter(true);
  };

  const handleResetSplit = () => {
    setSplitPercent(48);
    try {
      if (typeof window !== "undefined" && window.localStorage) {
        window.localStorage.setItem("sona:meeting-split-percent", "48");
      }
    } catch {
      // Ignore
    }
    showToast("已恢复默认双栏分屏比例 (48 : 52)");
  };

  useEffect(() => {
    if (!isDraggingSplitter) return;

    const handleMouseMove = (e: MouseEvent) => {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const relativeX = e.clientX - rect.left;
      const percent = (relativeX / rect.width) * 100;
      const clamped = Math.min(Math.max(percent, 25), 75);
      setSplitPercent(clamped);
    };

    const handleMouseUp = () => {
      setIsDraggingSplitter(false);
      try {
        if (typeof window !== "undefined" && window.localStorage) {
          window.localStorage.setItem("sona:meeting-split-percent", splitPercent.toFixed(1));
        }
      } catch {
        // Ignore
      }
    };

    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
    return () => {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isDraggingSplitter, splitPercent]);

  return (
    <div className="meeting-detail-view">
      {/* 顶部导航与全景信息栏 (使用统一的 detail-top-nav-bar 设计体系) */}
      <div className="detail-top-nav-bar">
        <div className="nav-left-section">
          {isMeetingActive && onReturnToActive && (
            <>
              <button
                type="button"
                className="detail-nav-back-btn detail-back-btn is-live-return"
                onClick={onReturnToActive}
                title="返回当前正在录制的会议工作台 (快捷键 Esc)"
              >
                <span className="live-rec-dot" />
                <ChevronLeftIcon size={13} />
                <span className="back-btn-text">返回正在进行的会议（{activeMeetingTitle || "当前会议"}）</span>
                <kbd className="nav-kbd-badge">Esc</kbd>
              </button>
              <div className="nav-section-divider" />
            </>
          )}

          <div className="detail-title-block">
            <div className="detail-title-row">
              <div className="detail-title-input-wrapper">
                <input
                  type="text"
                  className="detail-title-input"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  onBlur={() => void handleSaveTitle()}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      (e.target as HTMLInputElement).blur();
                    } else if (e.key === "Escape") {
                      setTitle(meeting.title);
                      (e.target as HTMLInputElement).blur();
                    }
                  }}
                  maxLength={200}
                  placeholder="未命名会议..."
                />
                <button
                  type="button"
                  className={`ai-title-gen-btn ${isGeneratingTitle ? "loading" : ""}`}
                  onClick={() => void handleGenerateTitle()}
                  disabled={isGeneratingTitle}
                  title="根据完整会议转录由 AI 提炼最佳标题"
                >
                  <span className="ai-btn-icon">✨</span>
                  <span className="ai-btn-text">
                    {isGeneratingTitle ? "提炼中..." : "AI 提炼标题"}
                  </span>
                </button>
              </div>

              {/* 仅在 recording 状态显示动态录制徽章，completed / interrupted 是默认语义无需赘述 */}
              {meeting.status === "recording" && (
                <span className={`status-badge-chip ${meeting.status}`}>
                  录制中
                </span>
              )}
            </div>

            <div className="detail-meta-row">
              <span className="detail-meta-pill">
                <span className="meta-icon">📅</span>
                {new Date(meeting.started_at || meeting.created_at).toLocaleString()}
              </span>
              <span className="detail-meta-pill">
                <span className="meta-icon">🎙️</span>
                {segments.length} 段发言
              </span>
              {starredIds && starredIds.size > 0 && (
                <span className="detail-meta-pill" style={{ color: "var(--color-yellow)" }}>
                  <span className="meta-icon">⭐</span>
                  {starredIds.size} 个重点发言
                </span>
              )}
            </div>
          </div>
        </div>

        <div className="nav-right-section">
          <div className="export-dropdown-wrapper">
            <button
              type="button"
              className={`detail-action-btn export-btn ${isExportMenuOpen ? "active" : ""}`}
              onClick={() => setIsExportMenuOpen((prev) => !prev)}
            >
              <span>📥 导出与分享</span>
              <span className="export-chevron">{isExportMenuOpen ? "▲" : "▼"}</span>
            </button>

            {isExportMenuOpen && (
              <div className="export-dropdown-menu">
                <div className="export-dropdown-header">导出与分享</div>
                <button
                  type="button"
                  className="export-menu-item highlight"
                  onClick={() => void handleCopyReport()}
                >
                  <span className="item-icon">📋</span>
                  <span className="item-label">复制会议汇报格式</span>
                </button>
                <button
                  type="button"
                  className="export-menu-item"
                  onClick={() => void handleCopyChecklist()}
                >
                  <span className="item-icon">☑️</span>
                  <span className="item-label">复制待办清单 (Checklist)</span>
                </button>
                {starredIds && starredIds.size > 0 && (
                  <button
                    type="button"
                    className="export-menu-item highlight"
                    onClick={() => void handleCopyStarred()}
                  >
                    <span className="item-icon">⭐</span>
                    <span className="item-label">复制重点发言 ({starredIds.size} 段)</span>
                  </button>
                )}
                <div className="export-dropdown-divider" />
                <button
                  type="button"
                  className="export-menu-item"
                  onClick={() => void handleExport("md")}
                >
                  <span className="item-icon">📝</span>
                  <span className="item-label">Markdown (.md)</span>
                </button>
                <button
                  type="button"
                  className="export-menu-item"
                  onClick={() => void handleExport("txt")}
                >
                  <span className="item-icon">📄</span>
                  <span className="item-label">纯文本 (.txt)</span>
                </button>
                <button
                  type="button"
                  className="export-menu-item"
                  onClick={() => void handleExport("srt")}
                >
                  <span className="item-icon">🎬</span>
                  <span className="item-label">SRT 字幕 (.srt)</span>
                </button>
                <button
                  type="button"
                  className="export-menu-item"
                  onClick={() => void handleExport("json")}
                >
                  <span className="item-icon">⚙️</span>
                  <span className="item-label">原始 JSON 时序 (.json)</span>
                </button>
              </div>
            )}
          </div>

          <button
            type="button"
            className="detail-action-btn delete-btn"
            onClick={() => void onDeleteMeeting()}
            title="永久删除此会议"
          >
            <span>🗑️</span>
          </button>
        </div>
      </div>

      {/* 双栏工作区 */}
      <div
        className={`dual-pane-grid ${isDraggingSplitter ? "is-resizing" : ""}`}
        ref={containerRef}
        style={{
          "--meeting-split-percent": `${splitPercent}%`,
        } as CSSProperties}
      >
        <MeetingTranscriptViewer
          segments={segments}
          highlightedSegmentId={highlightedSegmentId}
          onRenameSpeaker={onRenameSpeaker}
          starredIds={starredIds}
          onToggleStarSegment={onToggleStarSegment}
        />
        <div
          className="pane-splitter"
          onMouseDown={handleSplitterMouseDown}
          onDoubleClick={handleResetSplit}
          title="按住鼠标拖拽调节左右栏宽度，双击重置比例"
        >
          <div className="splitter-handle-bar" />
        </div>

        {/* 右侧多维面板 (Segmented Tabs: 纪要 / 内心 OS) */}
        <div className="detail-right-pane-wrapper">
          <div className="detail-segmented-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={activeRightTab === "minutes"}
              className={`detail-tab-btn ${activeRightTab === "minutes" ? "active" : ""}`}
              onClick={() => setActiveRightTab("minutes")}
            >
              ✨ AI 会议纪要
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={activeRightTab === "inner_os"}
              className={`detail-tab-btn tab-inneros ${activeRightTab === "inner_os" ? "active" : ""}`}
              onClick={() => setActiveRightTab("inner_os")}
            >
              🔒 内心 OS 问答档案
            </button>
          </div>

          <div className="detail-tab-content">
            {activeRightTab === "minutes" ? (
              <MeetingMinutesViewer
                minutes={minutes}
                minutesList={minutesList}
                selectedVersion={selectedMinutesVersion}
                onSelectVersion={onSelectMinutesVersion}
                onRegenerate={handleRegenerate}
                onSelectEvidence={handleEvidenceClick}
                isRegenerating={isRegenerating}
                hideTitle={true}
              />
            ) : (
              <InnerOSHistoryTab
                meetingId={meeting.id}
                onSelectEvidence={handleEvidenceClick}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
