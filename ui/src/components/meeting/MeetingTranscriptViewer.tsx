import React, { useMemo, useState, useEffect, useRef } from "react";
import type { TranscriptSegment } from "../../contracts/meetingContract";
import { formatTimeRange } from "./MeetingGapAlert";
import { showToast } from "../Toast";

interface MeetingTranscriptViewerProps {
  segments: readonly TranscriptSegment[];
  highlightedSegmentId: string | null;
  onRenameSpeaker: (speakerKey: string, currentName: string) => void;
  starredIds?: ReadonlySet<string>;
  onToggleStarSegment?: (segmentId: string) => void;
}

const SPEAKER_COLORS = [
  "#6366f1", // indigo
  "#10b981", // emerald
  "#f59e0b", // amber
  "#ec4899", // pink
  "#06b6d4", // cyan
  "#8b5cf6", // purple
];

function highlightMatch(text: string, query: string) {
  if (!query.trim()) return text;
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const parts = text.split(new RegExp(`(${escaped})`, "gi"));
  return (
    <>
      {parts.map((part, i) =>
        part.toLowerCase() === query.toLowerCase() ? (
          <mark key={i} className="transcript-search-highlight">
            {part}
          </mark>
        ) : (
          <React.Fragment key={i}>{part}</React.Fragment>
        ),
      )}
    </>
  );
}

export function MeetingTranscriptViewer({
  segments,
  highlightedSegmentId,
  onRenameSpeaker,
  starredIds: propStarredIds,
  onToggleStarSegment: propToggleStarSegment,
}: MeetingTranscriptViewerProps) {
  const [search, setSearch] = useState("");
  const [selectedSpeaker, setSelectedSpeaker] = useState<string>("all");
  const [localStarredIds, setLocalStarredIds] = useState<Set<string>>(() => new Set());
  const starredIds = propStarredIds ?? localStarredIds;
  const [filterStarredOnly, setFilterStarredOnly] = useState(false);
  const segmentRefs = useRef<Record<string, HTMLDivElement | null>>({});

  // Unique speakers and their statistics
  const speakerStats = useMemo(() => {
    const map = new Map<string, { key: string; name: string; count: number; chars: number }>();
    let totalSegments = segments.length;

    for (const seg of segments) {
      if (!map.has(seg.speaker_key)) {
        map.set(seg.speaker_key, { key: seg.speaker_key, name: seg.speaker_name, count: 0, chars: 0 });
      }
      const item = map.get(seg.speaker_key)!;
      item.count += 1;
      item.chars += seg.text.length;
    }

    const list = Array.from(map.values()).map((spk, idx) => ({
      ...spk,
      color: SPEAKER_COLORS[idx % SPEAKER_COLORS.length],
      percent: totalSegments > 0 ? Math.round((spk.count / totalSegments) * 100) : 0,
    }));

    return list;
  }, [segments]);

  const toggleStarSegment = (id: string) => {
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
  };

  // Filtered segments
  const filtered = useMemo(() => {
    return segments.filter((seg) => {
      if (filterStarredOnly && !starredIds.has(seg.id)) return false;
      const matchSpeaker = selectedSpeaker === "all" || seg.speaker_key === selectedSpeaker;
      const matchSearch =
        !search.trim() ||
        seg.text.toLowerCase().includes(search.toLowerCase()) ||
        seg.speaker_name.toLowerCase().includes(search.toLowerCase());
      return matchSpeaker && matchSearch;
    });
  }, [segments, filterStarredOnly, starredIds, selectedSpeaker, search]);

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
        <div className="pane-title-group">
          <span className="pane-icon">📝</span>
          <span className="pane-title">会议逐字转录</span>
          <span className="pane-count-badge">
            {filtered.length} 段
            {starredIds.size > 0 && ` · ⭐ ${starredIds.size} 重点`}
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          {starredIds.size > 0 && (
            <button
              type="button"
              className={`pane-header-btn ${filterStarredOnly ? "primary" : ""}`}
              onClick={() => setFilterStarredOnly((prev) => !prev)}
              title={filterStarredOnly ? "查看全部转录段落" : "仅查看已标为重点的段落"}
            >
              <span>⭐</span>
              <span>{filterStarredOnly ? "查看全部" : `仅看重点 (${starredIds.size})`}</span>
            </button>
          )}
          <button
            type="button"
            className="pane-header-btn"
            onClick={() => void handleCopyAll()}
            title="复制当前筛选的全部转录文本"
          >
            <span>📋</span>
            <span>复制全部</span>
          </button>
        </div>
      </div>

      {/* 说话人发言占比分布条 (Speaker Distribution Bar) */}
      {speakerStats.length > 0 && (
        <div className="speaker-distribution-container">
          <div className="speaker-distribution-bar" title="说话人发言段落占比">
            {speakerStats.map((spk) => (
              <div
                key={spk.key}
                className="distribution-segment"
                style={{
                  width: `${spk.percent}%`,
                  backgroundColor: spk.color,
                }}
                title={`${spk.name}: ${spk.count}段 (${spk.percent}%)`}
              />
            ))}
          </div>
          <div className="speaker-chips-row">
            {speakerStats.map((spk) => {
              const isSelected = selectedSpeaker === spk.key;
              return (
                <button
                  key={spk.key}
                  type="button"
                  className={`speaker-stat-chip ${isSelected ? "selected" : ""}`}
                  onClick={() => setSelectedSpeaker(isSelected ? "all" : spk.key)}
                  title={`点击按 ${spk.name} 筛选发言`}
                >
                  <span className="chip-dot" style={{ backgroundColor: spk.color }} />
                  <span className="chip-name">{spk.name}</span>
                  <span className="chip-percent">{spk.percent}% ({spk.count}段)</span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* 说话人通道超限提示 (超过 4 位说话人) */}
      {speakerStats.length > 4 && (
        <div className="speaker-overflow-warning">
          <span>⚠️</span>
          <span>
            当前已检测到 {speakerStats.length} 个说话人通道（推荐 ≤ 4 人）。超过 4 人可能存在声纹归属漂移，建议点击名字修正。
          </span>
        </div>
      )}

      <div className="pane-filter-bar">
        <div className="search-input-wrapper">
          <span className="search-icon">🔍</span>
          <input
            className="search-input"
            placeholder="搜索转录内容或说话人..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          {search && (
            <button
              type="button"
              className="search-clear-btn"
              onClick={() => setSearch("")}
              title="清空搜索"
            >
              ✕
            </button>
          )}
        </div>
        <select
          className="speaker-select"
          value={selectedSpeaker}
          onChange={(e) => setSelectedSpeaker(e.target.value)}
        >
          <option value="all">全部说话人 ({speakerStats.length})</option>
          {speakerStats.map((spk) => (
            <option key={spk.key} value={spk.key}>
              {spk.name} ({spk.count}段)
            </option>
          ))}
        </select>
      </div>

      <div className="segment-list">
        {filtered.length === 0 && (
          <div className="history-empty">
            <span style={{ fontSize: "1.5rem", marginBottom: "6px" }}>🔍</span>
            <span>
              {filterStarredOnly
                ? "暂无标记为重点的段落"
                : "未匹配到相关转录段落"}
            </span>
          </div>
        )}

        {filtered.map((seg) => {
          const isHighlighted = highlightedSegmentId === seg.id;
          const isStarred = starredIds.has(seg.id);
          const currentSpeakerColor =
            speakerStats.find((s) => s.key === seg.speaker_key)?.color || "var(--color-accent)";

          return (
            <div
              key={seg.id}
              ref={(el) => {
                segmentRefs.current[seg.id] = el;
              }}
              className={`segment-card ${isHighlighted ? "highlighted" : ""} ${isStarred ? "is-starred" : ""}`}
              style={{
                borderLeftColor: isStarred ? "var(--color-yellow)" : currentSpeakerColor,
              }}
            >
              <div className="segment-top">
                <button
                  type="button"
                  className="speaker-tag-btn"
                  onClick={() => onRenameSpeaker(seg.speaker_key, seg.speaker_name)}
                  title="点击修改此说话人名称"
                >
                  <span
                    className="speaker-avatar-circle"
                    style={{ backgroundColor: currentSpeakerColor }}
                  >
                    👤
                  </span>
                  <span className="speaker-name-text">{seg.speaker_name}</span>
                  <span className="speaker-edit-badge">✎</span>
                </button>
                <div className="segment-actions-group">
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
                    title={isStarred ? "取消重点标记" : "标记此段落为重点"}
                    onClick={() => toggleStarSegment(seg.id)}
                  >
                    <span>{isStarred ? "⭐" : "✩"}</span>
                    <span className="star-btn-text">{isStarred ? "已标记" : "标为重点"}</span>
                  </button>
                  <button
                    type="button"
                    className="segment-copy-btn"
                    title="复制此段内容"
                    onClick={() => void handleCopySegment(seg.text)}
                  >
                    📋
                  </button>
                </div>
              </div>
              <p className="segment-text">{highlightMatch(seg.text, search)}</p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
