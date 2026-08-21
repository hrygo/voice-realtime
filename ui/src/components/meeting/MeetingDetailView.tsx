import { useState } from "react";
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

      <div className="dual-pane-grid">
        <MeetingTranscriptViewer
          segments={segments}
          highlightedSegmentId={highlightedSegmentId}
          onRenameSpeaker={onRenameSpeaker}
        />
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
