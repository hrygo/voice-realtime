import { useState, useEffect, useRef, type CSSProperties } from "react";
import type {
  ExportFormat,
  MeetingDetail,
  MeetingMinutesVersion,
  TranscriptSegment,
} from "../../contracts/meetingContract";
import { MeetingTranscriptViewer } from "./MeetingTranscriptViewer";
import { MeetingMinutesViewer } from "./MeetingMinutesViewer";
import { meetingApi } from "../../services/meetingApi";
import { exportMeetingData } from "../../utils/exportUtils";
import { copyTextToClipboard } from "../../utils/clipboard";
import { showToast } from "../Toast";

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
  const [isEditingTitle, setIsEditingTitle] = useState(false);
  const [isGeneratingTitle, setIsGeneratingTitle] = useState(false);
  const [isExportMenuOpen, setIsExportMenuOpen] = useState(false);
  const [highlightedSegmentId, setHighlightedSegmentId] = useState<string | null>(null);
  const [isRegenerating, setIsRegenerating] = useState(false);

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

  // Split ratio (left pane percentage, e.g. 48)
  const [splitPercent, setSplitPercent] = useState<number>(() => {
    try {
      const stored = localStorage.getItem("voice-studio:meeting-split-ratio");
      if (stored) {
        const val = parseFloat(stored);
        if (val >= 25 && val <= 75) return val;
      }
    } catch {
      // Ignore
    }
    return 48;
  });

  const [isDraggingSplitter, setIsDraggingSplitter] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleSplitterMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsDraggingSplitter(true);
  };

  useEffect(() => {
    if (!isDraggingSplitter) return;

    const handleMouseMove = (e: MouseEvent) => {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      if (rect.width <= 0) return;
      const offset = e.clientX - rect.left;
      const pct = Math.min(75, Math.max(25, (offset / rect.width) * 100));
      setSplitPercent(pct);
      try {
        localStorage.setItem("voice-studio:meeting-split-ratio", pct.toFixed(1));
      } catch {
        // Ignore
      }
    };

    const handleMouseUp = () => {
      setIsDraggingSplitter(false);
    };

    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isDraggingSplitter]);

  const handleResetSplit = () => {
    setSplitPercent(48);
    try {
      localStorage.setItem("voice-studio:meeting-split-ratio", "48");
    } catch {
      // Ignore
    }
    showToast("双栏比例已重置为默认", "info");
  };

  // Sync title when selected meeting changes
  useEffect(() => {
    setTitle(meeting.title);
    setIsEditingTitle(false);
  }, [meeting.id, meeting.title]);

  // Click outside to close export menu
  useEffect(() => {
    if (!isExportMenuOpen) return;
    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (!target.closest(".export-dropdown-wrapper")) {
        setIsExportMenuOpen(false);
      }
    };
    window.addEventListener("click", handleClickOutside);
    return () => window.removeEventListener("click", handleClickOutside);
  }, [isExportMenuOpen]);

  const handleTitleBlur = async () => {
    setIsEditingTitle(false);
    const trimmed = title.trim();
    if (trimmed && trimmed !== meeting.title) {
      try {
        await onUpdateTitle(trimmed);
        showToast("会议标题已更新", "success");
      } catch (err) {
        showToast(err instanceof Error ? err.message : "标题修改失败", "error");
        setTitle(meeting.title);
      }
    } else {
      setTitle(meeting.title);
    }
  };

  const handleExport = async (format: ExportFormat) => {
    setIsExportMenuOpen(false);
    try {
      await meetingApi.downloadExport(meeting.id, format, meeting.title);
      showToast(`已导出 ${format.toUpperCase()} 文件`, "success");
    } catch {
      // 降级使用前端客户端格式化导出
      try {
        exportMeetingData(meeting, segments, minutes, format, starredIds);
        showToast(`已导出 ${format.toUpperCase()} 文件 (本地离线生成)`, "success");
      } catch (err) {
        showToast(err instanceof Error ? err.message : "导出失败", "error");
      }
    }
  };

  const handleCopyReport = async () => {
    setIsExportMenuOpen(false);
    if (!minutes?.content_json) {
      showToast("暂无可导出的结构化纪要", "warning");
      return;
    }
    const j = minutes.content_json;
    let report = `# 📢 会议总结: ${meeting.title}\n\n`;
    report += `**会议时间**: ${meeting.started_at ? new Date(meeting.started_at).toLocaleString() : "未知"}\n`;
    report += `**说话人**: ${Object.values(meeting.speakers || {}).map((s) => s.display_name).join(", ") || "发言人"}\n\n`;
    if (j.overview) report += `## 📋 会议概要\n${j.overview}\n\n`;
    if (j.topics?.length) {
      report += `## 💡 核心议题\n` + j.topics.map((t, idx) => `${idx + 1}. **${t.title}**: ${t.summary}`).join("\n") + "\n\n";
    }
    if (j.decisions?.length) {
      report += `## ✅ 决策事项\n` + j.decisions.map((d) => `- ${d.content}`).join("\n") + "\n\n";
    }
    if (j.action_items?.length) {
      report += `## 📌 待办行动项\n` + j.action_items.map((a) => `- [ ] ${a.task}${a.owner ? ` (@${a.owner})` : ""}${a.due_date ? ` (截止: ${a.due_date})` : ""}`).join("\n") + "\n\n";
    }
    try {
      await copyTextToClipboard(report);
      showToast("会议汇报排版已成功复制到剪贴板", "success");
    } catch {
      showToast("复制失败，请检查浏览器剪贴板权限", "warning");
    }
  };

  const handleCopyChecklist = async () => {
    setIsExportMenuOpen(false);
    if (!minutes?.content_json?.action_items?.length) {
      showToast("暂无可复制的待办事项", "warning");
      return;
    }
    const text = minutes.content_json.action_items
      .map((item) => `- [ ] ${item.task}${item.owner ? ` (@${item.owner})` : ""}${item.due_date ? ` (截止: ${item.due_date})` : ""}`)
      .join("\n");
    try {
      await copyTextToClipboard(text);
      showToast("待办事项清单 (Checklist) 已复制到剪贴板", "success");
    } catch {
      showToast("复制失败，请检查浏览器剪贴板权限", "warning");
    }
  };

  const handleCopyStarred = async () => {
    if (!starredIds || starredIds.size === 0) {
      showToast("当前会议暂无标记为重点的段落", "warning");
      return;
    }
    const starredSegs = segments.filter((s) => starredIds.has(s.id));
    const text = starredSegs.map((s) => `[${s.speaker_name}]: ${s.text}`).join("\n");
    try {
      await copyTextToClipboard(text);
      showToast(`已复制 ${starredSegs.length} 段重点发言`, "success");
      setIsExportMenuOpen(false);
    } catch {
      showToast("复制失败，请检查浏览器剪贴板权限", "warning");
    }
  };

  const handleRegenerate = async () => {
    setIsRegenerating(true);
    try {
      await onRegenerateMinutes();
      showToast("已提交重新生成纪要请求", "info");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "生成纪要失败", "error");
    } finally {
      setIsRegenerating(false);
    }
  };

  const handleEvidenceClick = (segmentId: string) => {
    setHighlightedSegmentId(segmentId);
    // 3秒后清除呼吸高亮效果
    setTimeout(() => {
      setHighlightedSegmentId((prev) => (prev === segmentId ? null : prev));
    }, 3000);
  };

function formatDuration(startedAt?: string | null, endedAt?: string | null): string {
  if (!startedAt) return "";
  const start = Date.parse(startedAt);
  const end = endedAt ? Date.parse(endedAt) : Date.now();
  if (isNaN(start) || isNaN(end) || end < start) return "";
  const totalSeconds = Math.max(0, Math.floor((end - start) / 1000));
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  if (h > 0) {
    return `${h}小时${m}分${s}秒`;
  }
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function formatMeetingDateTime(dateStr?: string | null): string {
  if (!dateStr) return "未知时间";
  try {
    const d = new Date(dateStr);
    return d.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "未知时间";
  }
}

  const durationText = formatDuration(meeting.started_at, meeting.ended_at);
  const speakerCount =
    Object.keys(meeting.speakers || {}).length ||
    new Set(segments.map((s) => s.speaker_key)).size ||
    1;

  return (
    <div className="meeting-detail-view">
      {/* 顶部现代化全景导航与会议信息栏 */}
      <header className="detail-top-nav-bar">
        <div className="nav-left-section">
          {isMeetingActive && onReturnToActive && (
            <>
              <button
                type="button"
                className="detail-nav-back-btn detail-back-btn is-live-return"
                onClick={onReturnToActive}
                title="返回正在进行的会议录制工作台 (按 Esc)"
              >
                <span className="back-arrow-icon">‹</span>
                <span className="back-btn-text">
                  <span className="live-rec-dot" />
                  <span>返回正在进行的会议{activeMeetingTitle ? `（${activeMeetingTitle}）` : ""}</span>
                </span>
                <kbd className="nav-kbd-badge">Esc</kbd>
              </button>
              <div className="nav-section-divider" />
            </>
          )}

          <div className="detail-title-block">
            <div className="detail-title-row">
              <div className={`detail-title-input-wrapper ${isEditingTitle || isGeneratingTitle ? "is-focused" : ""}`}>
                <input
                  className="detail-title-input"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  onFocus={() => setIsEditingTitle(true)}
                  onBlur={() => void handleTitleBlur()}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") e.currentTarget.blur();
                    if (e.key === "Escape") {
                      setTitle(meeting.title);
                      setIsEditingTitle(false);
                    }
                  }}
                  placeholder="输入会议主题..."
                  title="点击修改会议标题 (按 Enter 确认)"
                />
                <button
                  type="button"
                  className={`ai-title-gen-btn ${isGeneratingTitle ? "loading" : ""}`}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => void handleGenerateTitle()}
                  disabled={isGeneratingTitle || segments.length === 0}
                  title={segments.length === 0 ? "暂无转录内容，无法提炼标题" : "根据会议转录由 AI 提炼并更新会议标题"}
                >
                  <span className="ai-btn-icon">{isGeneratingTitle ? "⏳" : "✨"}</span>
                  <span className="ai-btn-text">{isGeneratingTitle ? "提炼中..." : "AI 智能命名"}</span>
                </button>
              </div>
              {isEditingTitle && (
                <span className="detail-save-hint">
                  (按 Enter 保存)
                </span>
              )}
            </div>

            <div className="detail-meta-row">
              <span className="detail-meta-pill">
                <span className="meta-icon">📅</span>
                <span>{formatMeetingDateTime(meeting.started_at)}</span>
              </span>
              {durationText && (
                <span className="detail-meta-pill">
                  <span className="meta-icon">⏱️</span>
                  <span>时长 {durationText}</span>
                </span>
              )}
              <span className="detail-meta-pill">
                <span className="meta-icon">🎙️</span>
                <span>{segments.length} 段发言</span>
              </span>
              <span className="detail-meta-pill">
                <span className="meta-icon">👥</span>
                <span>{speakerCount} 位发言人</span>
              </span>
            </div>
          </div>
        </div>

        <div className="nav-right-section">
          {/* 导出菜单 */}
          <div className="export-dropdown-wrapper">
            <button
              type="button"
              className={`detail-action-btn export-btn ${isExportMenuOpen ? "active" : ""}`}
              onClick={() => setIsExportMenuOpen((prev) => !prev)}
            >
              <span>📥 导出记录</span>
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
      </header>

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
        <MeetingMinutesViewer
          minutes={minutes}
          minutesList={minutesList}
          selectedVersion={selectedMinutesVersion}
          onSelectVersion={onSelectMinutesVersion}
          onRegenerate={handleRegenerate}
          onSelectEvidence={handleEvidenceClick}
          isRegenerating={isRegenerating}
        />
      </div>
    </div>
  );
}
