import { useEffect, useRef, useState, useCallback } from "react";
import type { TranscriptSegment } from "../../contracts/meetingContract";
import { formatTimeRange, MeetingGapAlert } from "./MeetingGapAlert";
import type { TranscriptionGap } from "../../stores/meetingStore";
import { showToast } from "../Toast";

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
}: MeetingRecordingViewProps) {
  const [elapsed, setElapsed] = useState(0);
  const [starredIds, setStarredIds] = useState<Set<string>>(() => new Set());
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
    setStarredIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
        showToast("已取消重点标记", "info");
      } else {
        next.add(id);
        showToast("⭐ 已标记为此段落为重点", "success");
      }
      return next;
    });
  }, []);

  const handleStarLatest = useCallback(() => {
    if (segments.length === 0) {
      showToast("暂无转录段落可标记", "warning");
      return;
    }
    const latest = segments[segments.length - 1];
    toggleStarSegment(latest.id);
  }, [segments, toggleStarSegment]);

  // Keyboard shortcut: 'm' / 'M' to toggle mic, 's' / 'S' to star latest segment
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
          handleStarLatest();
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onToggleMic, handleStarLatest]);

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

  return (
    <div className="recording-view">
      <div className="recording-banner">
        <span>⚠️ 会议助手模式运行中：已完全静音交互与回复，持续转录并记录到本机数据库</span>
        <button
          type="button"
          className="btn-secondary"
          onClick={onToggleMic}
          style={{ fontSize: "0.72rem", padding: "2px 8px" }}
        >
          {micMuted ? "🔇 解除静音 (M)" : "🎙️ 麦克风采集中 (M)"}
        </button>
      </div>

      <MeetingGapAlert gaps={gaps} />

      <div className="recording-toolbar">
        <div className="toolbar-left">
          <div className="recording-timer">
            <span className="recording-dot" />
            <span>{formatElapsed(elapsed)}</span>
          </div>
          <div className="recording-vu-meter" title={micMuted ? "麦克风已静音" : "麦克风音频采集中"}>
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
            className="btn-secondary btn-star-latest"
            onClick={handleStarLatest}
            title="将当前最新段落标为重点 (快捷键 S)"
            style={{ fontSize: "0.72rem", padding: "2px 8px", marginLeft: "4px" }}
          >
            <span>⭐ 标记重点 (S)</span>
          </button>
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

      <div
        className="live-transcript-container"
        ref={scrollRef}
        onScroll={handleScroll}
      >
        {segments.length === 0 && !partialText && (
          <div className="history-empty">
            <span>🎙️ 正在倾听发言... 请保持讲话，实时转录将在此展示</span>
          </div>
        )}

        {segments.map((seg) => {
          const isStarred = starredIds.has(seg.id);
          return (
            <div key={seg.id} className={`segment-card ${isStarred ? "is-starred" : ""}`}>
              <div className="segment-top">
                <button
                  type="button"
                  className="speaker-tag-btn"
                  title="点击修改此说话人名称"
                  onClick={() => onRenameSpeaker(seg.speaker_key, seg.speaker_name)}
                >
                  <span>👤</span>
                  <span>{seg.speaker_name}</span>
                  <span style={{ opacity: 0.6, fontSize: "0.68rem" }}>✎</span>
                </button>
                <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
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
                    className="status-icon-btn"
                    style={{ fontSize: "0.72rem", padding: "1px 4px", opacity: isStarred ? 1 : 0.6 }}
                    title={isStarred ? "取消重点标记" : "标为此段为重点 (S)"}
                    onClick={() => toggleStarSegment(seg.id)}
                  >
                    {isStarred ? "⭐" : "✩"}
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
