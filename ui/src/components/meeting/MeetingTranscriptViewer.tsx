import { useMemo, useState, useEffect, useRef } from "react";
import type { TranscriptSegment } from "../../contracts/meetingContract";
import { formatTimeRange } from "./MeetingGapAlert";
import { showToast } from "../Toast";

interface MeetingTranscriptViewerProps {
  segments: readonly TranscriptSegment[];
  highlightedSegmentId: string | null;
  onRenameSpeaker: (speakerKey: string, currentName: string) => void;
}

export function MeetingTranscriptViewer({
  segments,
  highlightedSegmentId,
  onRenameSpeaker,
}: MeetingTranscriptViewerProps) {
  const [search, setSearch] = useState("");
  const [selectedSpeaker, setSelectedSpeaker] = useState<string>("all");
  const segmentRefs = useRef<Record<string, HTMLDivElement | null>>({});

  // Unique speakers
  const speakers = useMemo(() => {
    const map = new Map<string, string>();
    for (const seg of segments) {
      if (!map.has(seg.speaker_key)) {
        map.set(seg.speaker_key, seg.speaker_name);
      }
    }
    return Array.from(map.entries()).map(([key, name]) => ({ key, name }));
  }, [segments]);

  // Filtered segments
  const filtered = useMemo(() => {
    return segments.filter((seg) => {
      const matchSpeaker = selectedSpeaker === "all" || seg.speaker_key === selectedSpeaker;
      const matchSearch =
        !search.trim() ||
        seg.text.toLowerCase().includes(search.toLowerCase()) ||
        seg.speaker_name.toLowerCase().includes(search.toLowerCase());
      return matchSpeaker && matchSearch;
    });
  }, [segments, selectedSpeaker, search]);

  // Auto scroll to highlighted segment
  useEffect(() => {
    if (highlightedSegmentId && segmentRefs.current[highlightedSegmentId]) {
      const el = segmentRefs.current[highlightedSegmentId];
      el?.scrollIntoView?.({ behavior: "smooth", block: "center" });
    }
  }, [highlightedSegmentId]);

  const handleCopySegment = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      showToast("转录文本已复制到剪贴板", "info");
    } catch {
      showToast("复制失败", "warning");
    }
  };

  const handleCopyAll = async () => {
    try {
      const fullText = filtered.map((s) => `[${s.speaker_name}]: ${s.text}`).join("\n");
      await navigator.clipboard.writeText(fullText);
      showToast("全量转录文本已复制", "success");
    } catch {
      showToast("复制失败", "warning");
    }
  };

  return (
    <div className="transcript-pane">
      <div className="pane-header">
        <span>📝 会议逐字转录 ({filtered.length} 段)</span>
        <button
          type="button"
          className="btn-secondary"
          style={{ fontSize: "0.72rem", padding: "2px 8px" }}
          onClick={() => void handleCopyAll()}
          title="复制当前筛选的全部转录"
        >
          📋 复制全部
        </button>
      </div>

      {/* §9 说话人通道超限提示 (超过 4 位说话人) */}
      {speakers.length > 4 && (
        <div
          style={{
            padding: "6px 12px",
            background: "rgba(245, 158, 11, 0.12)",
            borderBottom: "1px solid var(--color-yellow)",
            color: "var(--color-yellow)",
            fontSize: "0.72rem",
            display: "flex",
            alignItems: "center",
            gap: "6px",
          }}
        >
          <span>⚠️</span>
          <span>
            当前已检测到 {speakers.length} 个说话人通道（推荐 ≤ 4 人）。超过 4 人可能存在声纹归属漂移，建议点击名字修正。
          </span>
        </div>
      )}

      <div className="pane-filter-bar">
        <input
          className="search-input"
          placeholder="搜索转录内容或说话人..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className="speaker-select"
          value={selectedSpeaker}
          onChange={(e) => setSelectedSpeaker(e.target.value)}
        >
          <option value="all">全部说话人 ({speakers.length})</option>
          {speakers.map((spk) => (
            <option key={spk.key} value={spk.key}>
              {spk.name}
            </option>
          ))}
        </select>
      </div>

      <div className="segment-list">
        {filtered.length === 0 && (
          <div className="history-empty">未匹配到相关转录段落</div>
        )}

        {filtered.map((seg) => {
          const isHighlighted = highlightedSegmentId === seg.id;

          return (
            <div
              key={seg.id}
              ref={(el) => {
                segmentRefs.current[seg.id] = el;
              }}
              className={`segment-card ${isHighlighted ? "highlighted" : ""}`}
            >
              <div className="segment-top">
                <button
                  type="button"
                  className="speaker-tag-btn"
                  onClick={() => onRenameSpeaker(seg.speaker_key, seg.speaker_name)}
                  title="点击修改此说话人名称"
                >
                  <span>👤</span>
                  <span>{seg.speaker_name}</span>
                  <span style={{ opacity: 0.6, fontSize: "0.68rem" }}>✎</span>
                </button>
                <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                  <span className="segment-time">
                    {formatTimeRange(seg.start_ms, seg.end_ms)}
                  </span>
                  <button
                    type="button"
                    className="status-icon-btn"
                    style={{ fontSize: "0.68rem", padding: "1px 4px", opacity: 0.6 }}
                    title="复制此段内容"
                    onClick={() => void handleCopySegment(seg.text)}
                  >
                    📄
                  </button>
                </div>
              </div>
              <p className="segment-text">{seg.text}</p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
