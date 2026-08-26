import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import type { TranscriptSegment } from "../../contracts/meetingContract";
import { formatTimeRange, MeetingGapAlert } from "./MeetingGapAlert";
import type { TranscriptionGap } from "../../stores/meetingStore";
import { showToast } from "../Toast";
import { MeetingWaveform } from "./MeetingWaveform";
import { deriveReadingBlocks } from "./transcriptViewModel";

export const SPEAKER_COLORS = [
  "#6366f1", // indigo
  "#10b981", // emerald
  "#f59e0b", // amber
  "#ec4899", // pink
  "#06b6d4", // cyan
  "#8b5cf6", // purple
];

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
  isCalibrating?: boolean;
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
  isCalibrating = false,
  starredIds: propStarredIds,
  onToggleStarSegment: propToggleStarSegment,
}: MeetingRecordingViewProps) {
  const [elapsed, setElapsed] = useState(0);
  const [localStarredIds, setLocalStarredIds] = useState<Set<string>>(() => new Set());
  const starredIds = propStarredIds ?? localStarredIds;
  const [selectedSegmentId, setSelectedSegmentId] = useState<string | null>(null);
  const [filterStarredOnly, setFilterStarredOnly] = useState(false);
  const [viewMode, setViewMode] = useState<"timeline" | "reading">("timeline");
  const [expandedBlockIds, setExpandedBlockIds] = useState<Set<string>>(() => new Set());
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

  // Unique speaker color map
  const speakerColorMap = useMemo(() => {
    const map = new Map<string, string>();
    let colorIdx = 0;
    for (const seg of segments) {
      if (!map.has(seg.speaker_key)) {
        map.set(seg.speaker_key, SPEAKER_COLORS[colorIdx % SPEAKER_COLORS.length]!);
        colorIdx++;
      }
    }
    return map;
  }, [segments]);

  const uniqueSpeakersCount = useMemo(() => {
    const set = new Set(segments.map((s) => s.speaker_key));
    return Math.max(set.size, 1);
  }, [segments]);

  const toggleStarSegment = useCallback((id: string) => {
    if (propToggleStarSegment) {
      const isCurrentlyStarred = starredIds.has(id);
      propToggleStarSegment(id);
      showToast(isCurrentlyStarred ? "已取消重点标记" : "⭐ 已标记此发言为重点", isCurrentlyStarred ? "info" : "success");
      return;
    }
    setLocalStarredIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
        showToast("已取消重点标记", "info");
      } else {
        next.add(id);
        showToast("⭐ 已标记此发言为重点", "success");
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
      showToast("暂无已确认转录片段可标记", "warning");
      return;
    }
    const latest = segments[segments.length - 1];
    if (latest) {
      toggleStarSegment(latest.id);
    }
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
  }, [segments, partialText, autoScroll, viewMode]);

  const handleScroll = () => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    const isAtBottom = scrollHeight - (scrollTop + clientHeight) < 40;
    setAutoScroll(isAtBottom);
  };

  const displayedSegments = filterStarredOnly
    ? segments.filter((seg) => starredIds.has(seg.id))
    : segments;

  const readingBlocks = useMemo(() => {
    return deriveReadingBlocks(displayedSegments, starredIds);
  }, [displayedSegments, starredIds]);

  const toggleBlockExpand = (blockId: string) => {
    setExpandedBlockIds((prev) => {
      const next = new Set(prev);
      if (next.has(blockId)) {
        next.delete(blockId);
      } else {
        next.add(blockId);
      }
      return next;
    });
  };

  const handleCopyText = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      showToast("文本已复制到剪贴板", "info");
    } catch {
      showToast("复制失败", "warning");
    }
  };

  return (
    <div className="recording-view">
      <MeetingGapAlert gaps={gaps} />

      {/* Accessible Polite Status Live Region */}
      <div className="sr-only" role="status" aria-live="polite">
        {isCalibrating
          ? "正在校准转录基线"
          : isEnding
            ? "正在冲刷并封存会议转录"
            : micMuted
              ? "麦克风已静音"
              : "会议录制中，麦克风正常监听"}
      </div>

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

          <span className="recording-status-text" style={{ fontSize: "0.78rem", color: "var(--text-secondary)" }}>
            已确认 {segments.length} 个发言片段 · {uniqueSpeakersCount} 位匿名说话人 · {isCalibrating ? "正在校准" : "正在识别"}
            {starredIds.size > 0 && ` · ⭐ ${starredIds.size} 重点`}
          </span>

          {/* 时序视图 / 阅读视图切换器 */}
          <div className="view-mode-toggle-group" role="radiogroup" aria-label="转录视图切换">
            <button
              type="button"
              className={`btn-view-mode ${viewMode === "timeline" ? "active" : ""}`}
              onClick={() => setViewMode("timeline")}
              aria-pressed={viewMode === "timeline"}
              title="时序视图：按原始 ASR 确认片段逐条展示，适合审计"
            >
              ⏱️ 时序视图
            </button>
            <button
              type="button"
              className={`btn-view-mode ${viewMode === "reading" ? "active" : ""}`}
              onClick={() => setViewMode("reading")}
              aria-pressed={viewMode === "reading"}
              title="阅读视图：同说话人短停顿发言聚合展示，提升阅读连贯性"
            >
              📖 阅读视图
            </button>
          </div>

          <button
            type="button"
            className={`btn-secondary btn-star-action ${selectedSegmentId ? "has-selection" : ""}`}
            onClick={handleStarSelectedOrLatest}
            aria-pressed={Boolean(selectedSegmentId && starredIds.has(selectedSegmentId))}
            title={
              selectedSegmentId
                ? "将当前选中片段标记/取消重点 (快捷键 S)"
                : "将最新发言标记为重点 (或点击任意片段进行重点选择，快捷键 S)"
            }
            style={{ fontSize: "0.72rem", padding: "3px 10px", marginLeft: "4px" }}
          >
            <span>⭐ {selectedSegmentId ? "标记选中片段 (S)" : "标记重点 (S)"}</span>
          </button>

          {starredIds.size > 0 && (
            <button
              type="button"
              className={`btn-secondary filter-starred-toggle ${filterStarredOnly ? "active" : ""}`}
              onClick={() => setFilterStarredOnly((prev) => !prev)}
              title={filterStarredOnly ? "查看全部转录片段" : "仅查看已标记为重点的片段"}
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
              <span>⭐ 暂无标记为重点的发言片段，点击片段右侧星号可随时标记</span>
            ) : (
              <span>🎙️ 正在倾听发言... 请保持讲话，实时转录将在此展示</span>
            )}
          </div>
        )}

        {/* 1. 时序视图 (Timeline View) */}
        {viewMode === "timeline" &&
          displayedSegments.map((seg) => {
            const isStarred = starredIds.has(seg.id);
            const isSelected = selectedSegmentId === seg.id;
            const speakerColor = speakerColorMap.get(seg.speaker_key) || "var(--color-accent)";
            return (
              <div
                key={seg.id}
                className={`segment-card ${isStarred ? "is-starred" : ""} ${isSelected ? "is-selected" : ""}`}
                onClick={() => setSelectedSegmentId((curr) => (curr === seg.id ? null : seg.id))}
                title="点击可选中此发言片段以进行重点标记或复制"
                style={{ borderLeftColor: isStarred ? "var(--color-yellow)" : speakerColor }}
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
                    <span className="speaker-avatar-circle" style={{ backgroundColor: speakerColor }}>
                      👤
                    </span>
                    <span className="speaker-name-text">{seg.speaker_name}</span>
                    <span className="speaker-edit-badge" title="可重命名">✎</span>
                  </button>
                  <div className="segment-actions-group" style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                    {isStarred && (
                      <span className="segment-starred-badge" title="重点发言片段">
                        ⭐ 重点
                      </span>
                    )}
                    <span className="segment-time">
                      {formatTimeRange(seg.start_ms, seg.end_ms)}
                    </span>
                    <button
                      type="button"
                      className={`segment-star-btn ${isStarred ? "active" : ""}`}
                      title={isStarred ? "取消重点标记" : "标记此片段为重点 (快捷键 S)"}
                      aria-pressed={isStarred}
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleStarSegment(seg.id);
                      }}
                    >
                      <span>{isStarred ? "⭐" : "✩"}</span>
                      <span className="star-btn-text">{isStarred ? "已标记" : "标为重点"}</span>
                    </button>
                    <button
                      type="button"
                      className="segment-copy-btn"
                      title="复制此发言内容"
                      onClick={(e) => {
                        e.stopPropagation();
                        void handleCopyText(seg.text);
                      }}
                    >
                      📋
                    </button>
                  </div>
                </div>
                <p className="segment-text">{seg.text}</p>
              </div>
            );
          })}

        {/* 2. 阅读视图 (Reading View Blocks) */}
        {viewMode === "reading" &&
          readingBlocks.map((block) => {
            const isExpanded = expandedBlockIds.has(block.block_id);
            const speakerColor = speakerColorMap.get(block.speaker_key) || "var(--color-accent)";
            return (
              <div
                key={block.block_id}
                className={`segment-card reading-block-card ${block.isStarred ? "is-starred" : ""}`}
                style={{ borderLeftColor: block.isStarred ? "var(--color-yellow)" : speakerColor }}
              >
                <div className="segment-top">
                  <button
                    type="button"
                    className="speaker-tag-btn"
                    title="点击修改此说话人名称"
                    onClick={(e) => {
                      e.stopPropagation();
                      onRenameSpeaker(block.speaker_key, block.speaker_name);
                    }}
                  >
                    <span className="speaker-avatar-circle" style={{ backgroundColor: speakerColor }}>
                      👤
                    </span>
                    <span className="speaker-name-text">{block.speaker_name}</span>
                    <span className="speaker-edit-badge" title="可重命名">✎</span>
                  </button>
                  <div className="segment-actions-group" style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                    {block.isStarred && (
                      <span className="segment-starred-badge" title="包含重点发言片段">
                        ⭐ 重点
                      </span>
                    )}
                    <span className="segment-time">
                      {formatTimeRange(block.start_ms, block.end_ms)}
                    </span>
                    <button
                      type="button"
                      className="segment-copy-btn"
                      title="复制此段阅读内容"
                      onClick={(e) => {
                        e.stopPropagation();
                        void handleCopyText(block.text);
                      }}
                    >
                      📋
                    </button>
                  </div>
                </div>
                <p className="segment-text reading-block-text">{block.text}</p>
                {block.segment_ids.length > 1 && (
                  <div className="reading-block-meta">
                    <button
                      type="button"
                      className="btn-expand-segments"
                      onClick={() => toggleBlockExpand(block.block_id)}
                    >
                      <span>{isExpanded ? "收起发言片段明细 ▴" : `聚合了 ${block.segment_ids.length} 个连续发言片段 ▾`}</span>
                    </button>
                    {isExpanded && (
                      <div className="reading-block-segments-preview">
                        {block.segment_ids.map((id) => {
                          const seg = segments.find((s) => s.id === id);
                          if (!seg) return null;
                          return (
                            <div key={id} className="preview-subsegment">
                              <span className="preview-subsegment-time">[{formatTimeRange(seg.start_ms, seg.end_ms)}]</span>
                              <span className="preview-subsegment-text">{seg.text}</span>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}

        {/* 3. 正在识别的 Partial 卡片 (明确状态区分，不计入确认段落) */}
        {partialText && (
          <div className="partial-card" role="status" aria-label="实时识别中">
            <div className="partial-header">
              <span>✍️ 实时识别中 (未确认)</span>
              <span className="partial-speaker-tag">
                {partialSpeaker ? `(${partialSpeaker})` : "(说话人待确认)"}
              </span>
            </div>
            <p style={{ margin: 0, fontStyle: "italic", opacity: 0.9 }}>{partialText}</p>
          </div>
        )}
      </div>
    </div>
  );
}
