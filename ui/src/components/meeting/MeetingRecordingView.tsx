import { useEffect, useRef, useState } from "react";
import type { TranscriptSegment } from "../../contracts/meetingContract";
import { formatTimeRange, MeetingGapAlert } from "./MeetingGapAlert";
import type { TranscriptionGap } from "../../stores/meetingStore";

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
          <span style={{ fontSize: "0.78rem", color: "var(--text-secondary)" }}>
            已记录 {segments.length} 个转录段落
          </span>
        </div>

        <button
          type="button"
          className="btn-end-meeting"
          onClick={() => void onEndMeeting()}
          disabled={isEnding}
        >
          <span>⏹️</span>
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

        {segments.map((seg) => (
          <div key={seg.id} className="segment-card">
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
              <span className="segment-time">
                {formatTimeRange(seg.start_ms, seg.end_ms)}
              </span>
            </div>
            <p className="segment-text">{seg.text}</p>
          </div>
        ))}

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
