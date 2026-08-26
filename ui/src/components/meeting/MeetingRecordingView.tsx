import { useEffect, useRef, useState, useCallback } from "react";
import type { TranscriptSegment } from "../../contracts/meetingContract";
import { formatTimeRange, MeetingGapAlert } from "./MeetingGapAlert";
import type { TranscriptionGap } from "../../stores/meetingStore";
import { showToast } from "../Toast";
import { MeetingWaveform } from "./MeetingWaveform";


export function formatElapsed(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) {
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

interface MeetingRecordingViewProps {
  startedAt: string | null;
  segments: readonly TranscriptSegment[];
  partialText: string | null;
  partialSpeaker: string | null;
  gaps: readonly TranscriptionGap[];
  micMuted: boolean;
  onToggleMic: () => void;
  onEndMeeting: () => Promise<void>;
  onRenameSpeaker: (speakerKey: string, currentName: string) => void;
  isEnding: boolean;
  starredIds?: ReadonlySet<string>;
  onToggleStarSegment?: (segmentId: string) => void;
}

export function MeetingRecordingView({
  startedAt,
  segments,
  partialText,
  partialSpeaker,
  gaps,
  micMuted,
  onToggleMic,
  onEndMeeting,
  onRenameSpeaker,
  isEnding,
  starredIds: propStarredIds,
  onToggleStarSegment: propToggleStarSegment,
}: MeetingRecordingViewProps) {
  const [elapsed, setElapsed] = useState(0);
  const [localStarredIds, setLocalStarredIds] = useState<Set<string>>(() => new Set());
  const starredIds = propStarredIds ?? localStarredIds;
  const [selectedSegmentId, setSelectedSegmentId] = useState<string | null>(null);
  const [filterStarredOnly, setFilterStarredOnly] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  // Timer
  useEffect(() => {
    if (!startedAt) return;
    const startMs = Date.parse(startedAt);
    const update = () => {
      const diffSec = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
      setElapsed(diffSec);
    };
    update();
    const interval = setInterval(update, 1000);
    return () => clearInterval(interval);
  }, [startedAt]);

  const toggleStarSegment = useCallback((id: string) => {
    if (propToggleStarSegment) {
      const isCurrentlyStarred = starredIds.has(id);
      propToggleStarSegment(id);
      showToast(isCurrentlyStarred ? "已取消重点标记" : "⭐ 已标记此段落为重点", isCurrentlyStarred ? "info" : "success");
      return;
    }
    setLocalStarredIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
        showToast("已取消重点标记", "info");
      } else {
        next.add(id);
        showToast("⭐ 已标记此段落为重点", "success");
      }
      return next;
    });
  }, [propToggleStarSegment, starredIds]);

  const handleStarSelectedOrLatest = useCallback(() => {
    if (selectedSegmentId) {
      toggleStarSegment(selectedSegmentId);
      return;
    }
    if (segments.length === 0) {
      showToast("暂无转录段落可标记", "warning");
      return;
    }
    const latest = segments[segments.length - 1];
    toggleStarSegment(latest.id);
  }, [selectedSegmentId, segments, toggleStarSegment]);

  // Keyboard shortcut: 'm' / 'M' to toggle mic, 's' / 'S' to star selected/latest segment
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) {
        return;
      }
      if (e.key === "m" || e.key === "M") {
        e.preventDefault();
        onToggleMic();
      } else if (e.key === "s" || e.key === "S") {
        if (!e.metaKey && !e.ctrlKey) {
          e.preventDefault();
          handleStarSelectedOrLatest();
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onToggleMic, handleStarSelectedOrLatest]);

  // Auto-scroll on new segments / partial
  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [segments, partialText, autoScroll]);

  const handleScroll = () => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    const isAtBottom = scrollHeight - (scrollTop + clientHeight) < 40;
    setAutoScroll(isAtBottom);
  };

  const displayedSegments = filterStarredOnly
    ? segments.filter((seg) => starredIds.has(seg.id))
    : segments;

  return (
    <div className="recording-view">
      <MeetingGapAlert gaps={gaps} />

      <div className="recording-toolbar">
        <div className="toolbar-left">
          <div className="recording-timer">
            <span className="recording-dot" />
            <span>{formatElapsed(elapsed)}</span>
          </div>
          <div className="recording-vu-meter" title={micMuted ? "麦克风已静音 (快捷键 M)" : "麦克风音频采集中 (快捷键 M)"}>
            <span className={`rec-vu-bar ${micMuted ? "muted" : "active"}`} style={{ height: micMuted ? "3px" : "12px" }} />
            <span className={`rec-vu-bar ${micMuted ? "muted" : "active"}`} style={{ height: micMuted ? "3px" : "16px" }} />
            <span className={`rec-vu-bar ${micMuted ? "muted" : "active"}`} style={{ height: micMuted ? "3px" : "10px" }} />
            <span className={`rec-vu-bar ${micMuted ? "muted" : "active"}`} style={{ height: micMuted ? "3px" : "14px" }} />
          </div>
          <span style={{ fontSize: "0.78rem", color: "var(--text-secondary)" }}>
            已记录 {segments.length} 个段落
            {starredIds.size > 0 && ` · ⭐ ${starredIds.size} 重点`}
          </span>

          <button
            type="button"
            className={`btn-secondary btn-star-action ${selectedSegmentId ? "has-selection" : ""}`}
            onClick={handleStarSelectedOrLatest}
            title={
              selectedSegmentId
                ? "将当前选中段落标记/取消重点 (快捷键 S)"
                : "将最新发言标记为重点 (或点击任意段落进行重点选择，快捷键 S)"
            }
            style={{ fontSize: "0.72rem", padding: "3px 10px", marginLeft: "4px" }}
          >
            <span>⭐ {selectedSegmentId ? "标记选中段落 (S)" : "标记重点 (S)"}</span>
          </button>

          {starredIds.size > 0 && (
            <button
              type="button"
              className={`btn-secondary filter-starred-toggle ${filterStarredOnly ? "active" : ""}`}
              onClick={() => setFilterStarredOnly((prev) => !prev)}
              title={filterStarredOnly ? "查看全部转录段落" : "仅查看已标记为重点的段落"}
              style={{ fontSize: "0.72rem", padding: "3px 10px" }}
            >
              <span>{filterStarredOnly ? "📋 查看全部" : `⭐ 仅看重点 (${starredIds.size})`}</span>
            </button>
          )}
        </div>

        <button
          type="button"
          className={`btn-end-meeting ${isEnding ? "is-ending" : ""}`}
          onClick={() => void onEndMeeting()}
          disabled={isEnding}
        >
          {isEnding ? (
            <span className="btn-spinner-sm" />
          ) : (
            <span>⏹️</span>
          )}
          <span>{isEnding ? "正在冲刷并封存..." : "结束会议并生成纪要"}</span>
        </button>
      </div>

      {/* 会议录制高保真拾音与声纹分轨拟真波形 */}
      <div className="meeting-waveform-container">
        <MeetingWaveform
          isRecording={true}
          hasPartial={Boolean(partialText)}
          isMuted={micMuted}
          activeTextTrigger={partialText || segments.length}
        />
      </div>

      <div
        className="live-transcript-container"
        ref={scrollRef}
        onScroll={handleScroll}
      >
        {displayedSegments.length === 0 && !partialText && (
          <div className="history-empty">
            {filterStarredOnly ? (
              <span>⭐ 暂无标记为重点的段落，点击段落右侧星号可随时标记</span>
            ) : (
              <span>🎙️ 正在倾听发言... 请保持讲话，实时转录将在此展示</span>
            )}
          </div>
        )}

        {displayedSegments.map((seg) => {
          const isStarred = starredIds.has(seg.id);
          const isSelected = selectedSegmentId === seg.id;
          return (
            <div
              key={seg.id}
              className={`segment-card ${isStarred ? "is-starred" : ""} ${isSelected ? "is-selected" : ""}`}
              onClick={() => setSelectedSegmentId((curr) => (curr === seg.id ? null : seg.id))}
              title="点击可选中此段落以进行重点标记或复制"
            >
              <div className="segment-top">
                <button
                  type="button"
                  className="speaker-tag-btn"
                  title="点击修改此说话人名称"
                  onClick={(e) => {
                    e.stopPropagation();
                    onRenameSpeaker(seg.speaker_key, seg.speaker_name);
                  }}
                >
                  <span>👤</span>
                  <span>{seg.speaker_name}</span>
                  <span style={{ opacity: 0.6, fontSize: "0.68rem" }}>✎</span>
                </button>
                <div className="segment-actions-group" style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                  {isStarred && (
                    <span className="segment-starred-badge" title="重点发言段落">
                      ⭐ 重点
                    </span>
                  )}
                  <span className="segment-time">
                    {formatTimeRange(seg.start_ms, seg.end_ms)}
                  </span>
                  <button
                    type="button"
                    className={`segment-star-btn ${isStarred ? "active" : ""}`}
                    title={isStarred ? "取消重点标记" : "标记此段落为重点 (快捷键 S)"}
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleStarSegment(seg.id);
                    }}
                  >
                    <span>{isStarred ? "⭐" : "✩"}</span>
                    <span className="star-btn-text">{isStarred ? "已标记" : "标为重点"}</span>
                  </button>
                </div>
              </div>
              <p className="segment-text">{seg.text}</p>
            </div>
          );
        })}

        {partialText && (
          <div className="partial-card">
            <div className="partial-header">
              <span>✍️ 实时转录中</span>
              {partialSpeaker && <span>({partialSpeaker})</span>}
            </div>
            <p style={{ margin: 0 }}>{partialText}</p>
          </div>
        )}
      </div>
    </div>
  );
}
