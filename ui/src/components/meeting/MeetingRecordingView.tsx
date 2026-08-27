import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import type { TranscriptSegment } from "../../contracts/meetingContract";
import { formatTimeRange, MeetingGapAlert } from "./MeetingGapAlert";
import type { TranscriptionGap } from "../../stores/meetingStore";
import { showToast } from "../Toast";
import { MeetingWaveform } from "./MeetingWaveform";
import { deriveReadingBlocks } from "./transcriptViewModel";
import { copyTextToClipboard } from "../../utils/clipboard";
import { InnerOSPanel, useInnerOSStore } from "../../features/innerOS";

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

export interface MeetingRecordingViewProps {
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

  // Inner OS state
  const isInnerOSOpen = useInnerOSStore((s) => s.isPanelOpen);
  const toggleInnerOS = useInnerOSStore((s) => s.togglePanel);
  const queryStatus = useInnerOSStore((s) => s.queryStatus);
  const isGenerating = queryStatus === "generating" || queryStatus === "accepted";

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

  const toggleStarSegment = useCallback((segmentId: string) => {
    if (propToggleStarSegment) {
      propToggleStarSegment(segmentId);
    } else {
      setLocalStarredIds((prev) => {
        const next = new Set(prev);
        if (next.has(segmentId)) {
          next.delete(segmentId);
        } else {
          next.add(segmentId);
        }
        return next;
      });
    }
  }, [propToggleStarSegment]);

  const handleStarSelectedOrLatest = useCallback(() => {
    if (selectedSegmentId) {
      toggleStarSegment(selectedSegmentId);
      const isNowStarred = !starredIds.has(selectedSegmentId);
      showToast(isNowStarred ? "已将当前选中片段标记为重点 ⭐" : "已取消该片段重点标记");
      return;
    }
    if (segments.length > 0) {
      const latest = segments[segments.length - 1];
      toggleStarSegment(latest.id);
      const isNowStarred = !starredIds.has(latest.id);
      showToast(isNowStarred ? "已将最新发言标记为重点 ⭐" : "已取消最新发言重点标记");
    }
  }, [selectedSegmentId, segments, starredIds, toggleStarSegment]);

  // Keyboard shortcuts (Meta+K for InnerOS works everywhere, S/M only when not in input)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const isCmdOrCtrl = e.metaKey || e.ctrlKey;
      const isK = e.key.toLowerCase() === "k" || e.code === "KeyK";

      // 1. Meta shortcut ⌘+K: Always toggles Inner OS, even if focus is inside an input/textarea!
      if (isCmdOrCtrl && !e.altKey && !e.shiftKey && isK) {
        e.preventDefault();
        toggleInnerOS();
        return;
      }

      // 2. Single-character shortcuts (S, M) are only active when not typing in inputs
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable)) {
        return;
      }

      if (!isCmdOrCtrl && !e.altKey && !e.shiftKey) {
        if (e.key === "s" || e.key === "S" || e.code === "KeyS") {
          e.preventDefault();
          handleStarSelectedOrLatest();
        } else if (e.key === "m" || e.key === "M" || e.code === "KeyM") {
          e.preventDefault();
          onToggleMic();
          showToast(micMuted ? "麦克风已解除静音 🎙️" : "麦克风已静音 🔇");
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleStarSelectedOrLatest, onToggleMic, micMuted, toggleInnerOS]);

  // Auto-scroll when new segments arrive
  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [segments, partialText, autoScroll]);

  const handleScroll = () => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    const isNearBottom = scrollHeight - scrollTop - clientHeight < 60;
    setAutoScroll(isNearBottom);
  };

  const handleCopyText = async (text: string) => {
    await copyTextToClipboard(text);
    showToast("已复制发言内容到剪贴板 📋");
  };

  const displayedSegments = useMemo(() => {
    if (!filterStarredOnly) return segments;
    return segments.filter((s) => starredIds.has(s.id));
  }, [segments, starredIds, filterStarredOnly]);

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

  // Evidence smooth anchor and 3s annealing animation
  const handleSelectEvidence = useCallback((segmentId: string) => {
    const el = document.getElementById(`segment-${segmentId}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.remove("is-evidence-target-inneros");
      // Force reflow for re-triggering keyframe
      void el.offsetWidth;
      el.classList.add("is-evidence-target-inneros");
      setTimeout(() => {
        el.classList.remove("is-evidence-target-inneros");
      }, 3000);
    }
  }, []);

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

      {/* Recording Toolbar: Left (Status & VU) · Center (Views & Star) · Right (InnerOS & End) */}
      <div className="recording-toolbar">
        {/* Cluster 1 (Left): 状态与声学 */}
        <div className="toolbar-cluster cluster-status">
          <div className="recording-timer" title="当前会议录制时长">
            <span className="recording-dot" />
            <span>{formatElapsed(elapsed)}</span>
          </div>

          <div
            className="recording-vu-meter"
            title={micMuted ? "麦克风已静音 (快捷键 M)" : "麦克风音频采集中 (快捷键 M)"}
          >
            <span
              className={`rec-vu-bar ${micMuted ? "muted" : "active"}`}
              style={{ height: micMuted ? "3px" : "12px" }}
            />
            <span
              className={`rec-vu-bar ${micMuted ? "muted" : "active"}`}
              style={{ height: micMuted ? "3px" : "16px" }}
            />
            <span
              className={`rec-vu-bar ${micMuted ? "muted" : "active"}`}
              style={{ height: micMuted ? "3px" : "10px" }}
            />
            <span
              className={`rec-vu-bar ${micMuted ? "muted" : "active"}`}
              style={{ height: micMuted ? "3px" : "14px" }}
            />
          </div>

          <span className="recording-status-text">
            已确认 {segments.length} 个发言片段 · {uniqueSpeakersCount} 位说话人 · {isCalibrating ? "正在校准" : "正在识别"}
            {starredIds.size > 0 && ` · ⭐ ${starredIds.size} 重点`}
          </span>
        </div>

        {/* Cluster 2 (Center): 视图模式与重点 */}
        <div className="toolbar-cluster cluster-views">
          <div className="view-mode-toggle-group" role="radiogroup" aria-label="转录视图切换">
            <button
              type="button"
              className={`btn-view-mode ${viewMode === "timeline" ? "active" : ""}`}
              onClick={() => setViewMode("timeline")}
              aria-pressed={viewMode === "timeline"}
              title="时序视图：按原始 ASR 确认片段逐条展示"
            >
              ⏱️ 时序视图
            </button>
            <button
              type="button"
              className={`btn-view-mode ${viewMode === "reading" ? "active" : ""}`}
              onClick={() => setViewMode("reading")}
              aria-pressed={viewMode === "reading"}
              title="阅读视图：聚合连续发言提升连贯性"
            >
              📖 阅读视图
            </button>
          </div>

          <div className="toolbar-cluster-divider" />

          <button
            type="button"
            className={`btn-toolbar-action btn-star-action ${selectedSegmentId ? "has-selection" : ""}`}
            onClick={handleStarSelectedOrLatest}
            aria-pressed={Boolean(selectedSegmentId && starredIds.has(selectedSegmentId))}
            title="标记重点发言 (快捷键 S)"
          >
            <span className="btn-star-icon">⭐</span>
            <span>{selectedSegmentId ? "标选中段" : "标重点"}</span>
            <kbd className="toolbar-kbd">S</kbd>
          </button>

          {starredIds.size > 0 && (
            <button
              type="button"
              className={`btn-toolbar-pill filter-starred-toggle ${filterStarredOnly ? "active" : ""}`}
              onClick={() => setFilterStarredOnly((prev) => !prev)}
              title={filterStarredOnly ? "查看全部转录片段" : "仅查看重点片段"}
            >
              <span>{filterStarredOnly ? "📋 全部" : `⭐ 重点 (${starredIds.size})`}</span>
            </button>
          )}
        </div>

        {/* Cluster 3 (Right): 副驾驶 & 结束 — 麦克风已由顶部 StatusBar VU 控件统一管控，此处不重复 */}
        <div className="toolbar-cluster cluster-actions">
          <button
            type="button"
            className={`btn-inneros-toggle ${isInnerOSOpen ? "is-active" : ""} ${isGenerating ? "is-generating" : ""}`}
            onClick={toggleInnerOS}
            title="展开/收起内心 OS 私密副驾驶 (⌘+K)"
            aria-pressed={isInnerOSOpen}
          >
            <span className="btn-inneros-icon">🔒</span>
            <span>内心 OS</span>
            <kbd className="toolbar-kbd inneros-kbd">⌘K</kbd>
            {isGenerating && <span className="btn-inneros-pulse" />}
          </button>

          <button
            type="button"
            className={`btn-end-meeting ${isEnding ? "is-ending" : ""}`}
            onClick={() => void onEndMeeting()}
            disabled={isEnding}
            title="结束当前会议并冲刷转录生成 AI 纪要"
          >
            {isEnding ? (
              <span className="btn-spinner-sm" />
            ) : (
              <span>⏹️</span>
            )}
            <span>{isEnding ? "正在冲刷并封存..." : "结束会议"}</span>
          </button>
        </div>
      </div>

      {/* Main Workspace Body Split with Inner OS */}
      <div className="recording-workspace-body">
        <div className="recording-transcript-pane">
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
                    id={`segment-${seg.id}`}
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
                      </div>
                    )}
                    {isExpanded && (
                      <div className="reading-block-subsegments">
                        {segments
                          .filter((sub) => block.segment_ids.includes(sub.id))
                          .map((sub) => {
                            const isSubStarred = starredIds.has(sub.id);
                            return (
                              <div
                                key={sub.id}
                                id={`segment-${sub.id}`}
                                className={`subsegment-item ${isSubStarred ? "is-starred" : ""}`}
                              >
                                <span className="subsegment-time">
                                  {formatTimeRange(sub.start_ms, sub.end_ms)}
                                </span>
                                <span className="subsegment-text">{sub.text}</span>
                                <button
                                  type="button"
                                  className={`segment-star-btn ${isSubStarred ? "active" : ""}`}
                                  onClick={() => toggleStarSegment(sub.id)}
                                  title={isSubStarred ? "取消重点标记" : "标记为重点"}
                                >
                                  {isSubStarred ? "⭐" : "✩"}
                                </button>
                              </div>
                            );
                          })}
                      </div>
                    )}
                  </div>
                );
              })}

            {/* 实时未定稿 ASR 气泡 (Partial transcript bubble) */}
            {partialText && (
              <div className="segment-card partial-card" style={{ borderLeftColor: "var(--color-yellow)" }}>
                <div className="segment-top">
                  <span className="speaker-tag-btn" style={{ cursor: "default" }}>
                    <span className="speaker-avatar-circle" style={{ backgroundColor: "var(--color-yellow)" }}>
                      ⏳
                    </span>
                    <span className="speaker-name-text">
                      {partialSpeaker || "正在识别说话人..."}
                    </span>
                  </span>
                  <span className="partial-badge">实时识别中</span>
                </div>
                <p className="segment-text partial-text">{partialText}</p>
              </div>
            )}
          </div>
        </div>

        {/* Right side Inner OS Panel */}
        <InnerOSPanel onSelectEvidence={handleSelectEvidence} />
      </div>
    </div>
  );
}
