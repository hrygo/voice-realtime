import { useState, useEffect, useRef } from "react";
import type {
  ExportFormat,
  MeetingDetail,
  MeetingMinutesVersion,
  TranscriptSegment,
} from "../../contracts/meetingContract";
import { getStatusLabel } from "./MeetingHistorySidebar";
import { MeetingTranscriptViewer } from "./MeetingTranscriptViewer";
import { MeetingMinutesViewer } from "./MeetingMinutesViewer";
import { meetingApi } from "../../services/meetingApi";
import { exportMeetingData } from "../../utils/exportUtils";
import { showToast } from "../Toast";

interface MeetingDetailViewProps {
  meeting: MeetingDetail;
  segments: readonly TranscriptSegment[];
  minutes: MeetingMinutesVersion | null;
  minutesList: readonly MeetingMinutesVersion[];
  selectedMinutesVersion: number | null;
  onSelectMinutesVersion: (version: number) => void;
  onUpdateTitle: (title: string) => Promise<void>;
  onRenameSpeaker: (speakerKey: string, currentName: string) => void;
  onRegenerateMinutes: () => Promise<void>;
  onDeleteMeeting: () => Promise<void> | void;
}

export function MeetingDetailView({
  meeting,
  segments,
  minutes,
  minutesList,
  selectedMinutesVersion,
  onSelectMinutesVersion,
  onUpdateTitle,
  onRenameSpeaker,
  onRegenerateMinutes,
  onDeleteMeeting,
}: MeetingDetailViewProps) {
  const [title, setTitle] = useState(meeting.title);
  const [isEditingTitle, setIsEditingTitle] = useState(false);
  const [isExportMenuOpen, setIsExportMenuOpen] = useState(false);
  const [highlightedSegmentId, setHighlightedSegmentId] = useState<string | null>(null);
  const [isRegenerating, setIsRegenerating] = useState(false);

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

  const statusInfo = getStatusLabel(meeting.status);

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
        exportMeetingData(meeting, segments, minutes, format);
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
    await navigator.clipboard.writeText(report);
    showToast("会议汇报排版已成功复制到剪贴板", "success");
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

  return (
    <div className="meeting-detail-view">
      <div className="detail-header">
        <div className="detail-title-group">
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
            title="点击修改会议标题 (按 Enter 确认)"
          />
          <span className={`status-badge ${statusInfo.className}`}>
            {statusInfo.text}
          </span>
          {isEditingTitle && (
            <span style={{ fontSize: "0.72rem", color: "var(--color-accent-light)" }}>
              (按回车保存)
            </span>
          )}
        </div>

        <div className="detail-actions">
          {/* 导出菜单 */}
          <div className="export-dropdown-wrapper">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setIsExportMenuOpen((prev) => !prev)}
            >
              <span>📥 导出</span>
              <span style={{ fontSize: "0.65rem" }}>▼</span>
            </button>

            {isExportMenuOpen && (
              <div className="export-menu">
                <button
                  type="button"
                  className="export-item"
                  onClick={() => void handleCopyReport()}
                >
                  📋 复制汇报格式
                </button>
                <button
                  type="button"
                  className="export-item"
                  onClick={() => void handleExport("md")}
                >
                  📝 Markdown (.md)
                </button>
                <button
                  type="button"
                  className="export-item"
                  onClick={() => void handleExport("txt")}
                >
                  📄 纯文本 (.txt)
                </button>
                <button
                  type="button"
                  className="export-item"
                  onClick={() => void handleExport("srt")}
                >
                  🎬 SRT 字幕 (.srt)
                </button>
                <button
                  type="button"
                  className="export-item"
                  onClick={() => void handleExport("json")}
                >
                  ⚙️ 原始 JSON (.json)
                </button>
              </div>
            )}
          </div>

          <button
            type="button"
            className="btn-secondary"
            style={{ color: "var(--color-red)" }}
            onClick={() => void onDeleteMeeting()}
            title="删除会议"
          >
            🗑️
          </button>
        </div>
      </div>

      <div
        className={`dual-pane-grid ${isDraggingSplitter ? "is-resizing" : ""}`}
        ref={containerRef}
        style={{
          gridTemplateColumns: `${splitPercent}% 6px calc(${100 - splitPercent}% - 6px)`,
        }}
      >
        <MeetingTranscriptViewer
          segments={segments}
          highlightedSegmentId={highlightedSegmentId}
          onRenameSpeaker={onRenameSpeaker}
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
