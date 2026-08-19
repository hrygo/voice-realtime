import React, { useState, useEffect, useRef, useMemo, useCallback } from "react";
import { useEventSocket } from "../hooks/useEventSocket";
import {
  speakerColor,
  toSRT,
  toMarkdownNotes,
  useSubtitleStore,
  type SubtitleLine,
} from "../stores/subtitleStore";
import { selectAssistantPhase, useAssistantStore } from "../stores/assistantStore";
import { useUISettingsStore } from "../stores/uiSettingsStore";
import { showToast } from "./Toast";
import "./SubtitleStream.css";

type FontSizeMode = "normal" | "medium" | "large";

function downloadBlob(content: string, filename: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export default function SubtitleStream() {
  const { lines, partial, connected, starredIndices, toggleStar } = useSubtitleStore();
  const assistantPhase = useAssistantStore(selectAssistantPhase);
  const teleprompterSettings = useUISettingsStore((s) => s.teleprompterSettings);
  const setTeleprompterSettings = useUISettingsStore((s) => s.setTeleprompterSettings);

  const scrollRef = useRef<HTMLDivElement>(null);
  const presentationScrollRef = useRef<HTMLDivElement>(null);

  /* ---- 工具栏状态 ---- */
  const [searchQuery, setSearchQuery] = useState("");
  const [speakerFilter, setSpeakerFilter] = useState<string>("all");
  const [fontSizeMode, setFontSizeMode] = useState<FontSizeMode>("normal");
  const [presentationMode, setPresentationMode] = useState(false);

  const handleMessage = useCallback((evt: MessageEvent) => {
    try {
      const payload = JSON.parse(evt.data as string);
      useSubtitleStore.getState().applySnapshot(payload);
    } catch {
      // Ignore
    }
  }, []);

  const { state } = useEventSocket("/ws/subtitles", handleMessage);

  useEffect(() => {
    useSubtitleStore.getState().setConnected(state === "open");
  }, [state]);

  // Auto-scroll
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;

    const presEl = presentationScrollRef.current;
    if (presEl) presEl.scrollTop = presEl.scrollHeight;
  }, [lines, partial]);

  // Available unique speakers
  const availableSpeakers = useMemo(() => {
    const set = new Set<number>();
    lines.forEach((l) => set.add(l.speaker));
    return Array.from(set).sort((a, b) => a - b);
  }, [lines]);

  // Filter logic
  const filteredLines = useMemo(() => {
    return lines
      .map((line, originalIndex) => ({ line, originalIndex }))
      .filter(({ line, originalIndex }) => {
        if (speakerFilter === "starred") {
          if (!starredIndices.has(originalIndex)) return false;
        } else if (speakerFilter !== "all") {
          if (String(line.speaker) !== speakerFilter) return false;
        }

        const q = searchQuery.trim().toLowerCase();
        if (!q) return true;
        return (
          line.text.toLowerCase().includes(q) ||
          Boolean(line.translation && line.translation.toLowerCase().includes(q))
        );
      });
  }, [lines, speakerFilter, searchQuery, starredIndices]);

  /* ---- 导出操作 ---- */
  const handleExportMarkdown = useCallback(() => {
    if (!lines.length) return;
    const content = toMarkdownNotes(lines, starredIndices);
    downloadBlob(
      content,
      `meeting-notes-${new Date().toISOString().substring(0, 10)}.md`,
      "text/markdown",
    );
    showToast("Markdown 会议纪要已成功导出", "success");
  }, [lines, starredIndices]);

  const handleExportSRT = useCallback(() => {
    if (!lines.length) return;
    const content = toSRT(lines);
    downloadBlob(
      content,
      `subtitles-${new Date().toISOString().substring(0, 19).replace(/:/g, "-")}.srt`,
      "application/x-subrip",
    );
    showToast("SRT 字幕文件已成功导出", "success");
  }, [lines]);

  const handleExportTXT = useCallback(() => {
    if (!lines.length) return;
    const content = lines
      .map((l) => `[${l.start} - ${l.end}] 说话人${l.speaker}: ${l.text}`)
      .join("\n");
    downloadBlob(
      content,
      `transcript-${new Date().toISOString().substring(0, 19).replace(/:/g, "-")}.txt`,
      "text/plain",
    );
    showToast("纯文本字幕已成功导出", "success");
  }, [lines]);

  const handleExportJSON = useCallback(() => {
    if (!lines.length) return;
    const content = JSON.stringify(lines, null, 2);
    downloadBlob(
      content,
      `subtitles-${new Date().toISOString().substring(0, 19).replace(/:/g, "-")}.json`,
      "application/json",
    );
    showToast("JSON 时序数据已成功导出", "success");
  }, [lines]);

  const handleCopyAll = useCallback(() => {
    if (!lines.length) return;
    const content = lines.map((l) => `说话人${l.speaker}: ${l.text}`).join("\n");
    navigator.clipboard.writeText(content).then(
      () => showToast("所有字幕文本已复制到剪贴板", "success"),
      () => showToast("复制失败", "error"),
    );
  }, [lines]);

  const handleClear = useCallback(() => {
    useSubtitleStore.getState().clear();
    showToast("字幕列表已清空", "info");
  }, []);

  const cycleFontSize = useCallback(() => {
    setFontSizeMode((prev) => {
      if (prev === "normal") return "medium";
      if (prev === "medium") return "large";
      return "normal";
    });
  }, []);

  // Keyboard Shortcuts
  useEffect(() => {
    const handleShortcuts = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "s") {
        e.preventDefault();
        handleExportSRT();
      } else if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "m") {
        e.preventDefault();
        handleExportMarkdown();
      } else if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "p") {
        e.preventDefault();
        setPresentationMode((prev) => !prev);
      }
    };
    window.addEventListener("keydown", handleShortcuts);
    return () => window.removeEventListener("keydown", handleShortcuts);
  }, [handleExportSRT, handleExportMarkdown]);

  return (
    <section
      className={`panel subtitle-panel ${assistantPhase === "speaking" ? "assistant-active" : ""}`}
      aria-label="实时字幕"
    >
      {/* 头部 */}
      <header className="panel-header subtitle-header">
        <div className="subtitle-header-left">
          <h2>
            <span>📝</span> 实时字幕
          </h2>
          <span className={`subtitle-status-pill ${connected ? "connected" : ""}`}>
            <span className="subtitle-status-dot" />
            {connected ? "WhisperLiveKit 已连接" : "等待连接"}
          </span>
        </div>

        <div className="subtitle-header-right">
          <button
            type="button"
            className="presentation-mode-btn"
            onClick={() => setPresentationMode(true)}
            title="进入舞台提词大字/光学镜像模式 (Cmd+Shift+P)"
          >
            <span>📺</span> 提词大字
          </button>
        </div>
      </header>

      {/* 搜索与过滤工具栏 */}
      <div className="subtitle-tools-bar">
        <div className="subtitle-search-wrap">
          <span className="subtitle-search-icon">🔍</span>
          <input
            type="text"
            className="subtitle-search-input"
            placeholder="搜索字幕文本..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
          {searchQuery && (
            <button
              type="button"
              className="subtitle-search-clear"
              onClick={() => setSearchQuery("")}
            >
              ✕
            </button>
          )}
        </div>

        <div className="subtitle-filter-group">
          <select
            className="subtitle-speaker-select"
            value={speakerFilter}
            onChange={(e) => setSpeakerFilter(e.target.value)}
            aria-label="筛选说话人或星标"
          >
            <option value="all">全部说话人</option>
            <option value="starred">⭐ 仅看星标重点 ({starredIndices.size})</option>
            {availableSpeakers.map((spk) => (
              <option key={spk} value={String(spk)}>
                👤 说话人 {spk}
              </option>
            ))}
          </select>

          <button
            type="button"
            className="font-size-toggle-btn"
            onClick={cycleFontSize}
            title="调节字幕字号大小"
          >
            {fontSizeMode === "normal" && "A (小)"}
            {fontSizeMode === "medium" && "A+ (中)"}
            {fontSizeMode === "large" && "A++ (大)"}
          </button>
        </div>
      </div>

      {/* 字幕内容流 */}
      <div
        className={`subtitle-stream-body font-${fontSizeMode}`}
        ref={scrollRef}
        aria-live="polite"
      >
        {filteredLines.map(({ line, originalIndex }) => (
          <SubtitleRow
            key={originalIndex}
            line={line}
            query={searchQuery}
            isStarred={starredIndices.has(originalIndex)}
            onToggleStar={() => toggleStar(originalIndex)}
          />
        ))}

        {partial && (
          <div className="subtitle-partial-row">
            <span className="partial-pulse-indicator" />
            <p className="subtitle-partial-text">{partial}</p>
          </div>
        )}

        {!lines.length && !partial && (
          <div className="subtitle-empty-wrap">
            <span className="subtitle-empty-icon">🎙️</span>
            <p className="subtitle-empty-title">等待语音字幕...</p>
            <p className="subtitle-empty-desc">
              WhisperLiveKit 流式 ASR 正在监听，系统检测到发言后将实时输出带说话人分离的字幕。
            </p>
          </div>
        )}
      </div>

      {/* 底部操作栏 */}
      <footer className="subtitle-bottom-toolbar">
        <div className="subtitle-actions-group">
          <button
            type="button"
            className="btn-ctrl"
            onClick={handleCopyAll}
            disabled={!lines.length}
            title="一键复制全部纯文本字幕"
          >
            <span>📋</span> 复制
          </button>
          <button
            type="button"
            className="btn-ctrl"
            onClick={handleExportMarkdown}
            disabled={!lines.length}
            title="导出为结构化 Markdown 会议纪要 (Cmd+Shift+M)"
          >
            <span>📝</span> 纪要 (MD)
          </button>
          <button
            type="button"
            className="btn-ctrl"
            onClick={handleExportSRT}
            disabled={!lines.length}
            title="导出为标准 SRT 字幕文件 (Cmd+Shift+S)"
          >
            <span>💾</span> SRT
          </button>
          <button
            type="button"
            className="btn-ctrl"
            onClick={handleExportTXT}
            disabled={!lines.length}
            title="导出为纯文本文件"
          >
            <span>📄</span> TXT
          </button>
          <button
            type="button"
            className="btn-ctrl"
            onClick={handleExportJSON}
            disabled={!lines.length}
            title="导出为 JSON 时序数据"
          >
            <span>📊</span> JSON
          </button>
          <button
            type="button"
            className="btn-ctrl btn-ctrl-danger"
            onClick={handleClear}
            disabled={!lines.length}
            title="清空当前字幕列表"
          >
            <span>🗑️</span> 清空
          </button>
        </div>

        <div className="subtitle-meta-stats">
          <span>{lines.length} 行</span>
          {starredIndices.size > 0 && <span>(⭐ {starredIndices.size})</span>}
        </div>
      </footer>

      {/* 演讲/提词器光学镜像大字模式 Modal */}
      {presentationMode && (
        <div
          className="presentation-overlay"
          role="dialog"
          aria-modal="true"
          onClick={() => setPresentationMode(false)}
        >
          <div className="presentation-header" onClick={(e) => e.stopPropagation()}>
            <div className="presentation-title-wrap">
              <span style={{ fontSize: "1.6rem" }}>🎙️</span>
              <h2>Voice Studio 舞台提词与字幕大屏</h2>
            </div>

            <div className="presentation-ctrls">
              <div className="presentation-font-slider-wrap">
                <span>字号</span>
                <input
                  type="range"
                  className="presentation-font-slider"
                  min="1.4"
                  max="3.6"
                  step="0.2"
                  value={teleprompterSettings.fontSize}
                  onChange={(e) =>
                    setTeleprompterSettings({ fontSize: parseFloat(e.target.value) })
                  }
                />
                <span>{teleprompterSettings.fontSize}rem</span>
              </div>

              <button
                type="button"
                className={`presentation-mirror-btn ${teleprompterSettings.mirror ? "active" : ""}`}
                onClick={() =>
                  setTeleprompterSettings({ mirror: !teleprompterSettings.mirror })
                }
                title="开启/关闭水平镜像翻转 (支持物理提词分光镜)"
              >
                🪞 {teleprompterSettings.mirror ? "镜像已开启" : "光学镜像"}
              </button>

              <button
                type="button"
                className="presentation-close-btn"
                onClick={() => setPresentationMode(false)}
              >
                退出 (Esc)
              </button>
            </div>
          </div>

          <div
            className={`presentation-body ${teleprompterSettings.mirror ? "mirror-mode" : ""}`}
            ref={presentationScrollRef}
            style={{ fontSize: `${teleprompterSettings.fontSize}rem` }}
            onClick={(e) => e.stopPropagation()}
          >
            {lines.map((line, idx) => (
              <div className="presentation-line" key={idx}>
                <span
                  className="presentation-speaker-tag"
                  style={{ color: speakerColor(line.speaker) }}
                >
                  {line.speaker >= 0 ? `👤 说话人 ${line.speaker}` : ""}
                </span>
                <span>{line.text}</span>
              </div>
            ))}
            {partial && (
              <div className="presentation-partial">
                <span>… {partial}</span>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function SubtitleRow({
  line,
  query,
  isStarred,
  onToggleStar,
}: {
  line: SubtitleLine;
  query: string;
  isStarred: boolean;
  onToggleStar: () => void;
}) {
  const highlightText = (text: string, q: string) => {
    if (!q.trim()) return text;
    const parts = text.split(new RegExp(`(${escapeRegExp(q)})`, "gi"));
    return (
      <>
        {parts.map((part, idx) =>
          part.toLowerCase() === q.toLowerCase() ? (
            <mark key={idx} className="subtitle-highlight">
              {part}
            </mark>
          ) : (
            <React.Fragment key={idx}>{part}</React.Fragment>
          ),
        )}
      </>
    );
  };

  const handleCopySingle = () => {
    navigator.clipboard.writeText(`[${line.start}] 说话人${line.speaker}: ${line.text}`).then(
      () => showToast("已复制单条字幕", "success"),
      () => showToast("复制失败", "error"),
    );
  };

  return (
    <div className={`subtitle-row-card ${isStarred ? "is-starred" : ""}`}>
      <div className="subtitle-row-header">
        <span
          className="subtitle-speaker-badge"
          style={{ color: speakerColor(line.speaker) }}
        >
          {line.speaker >= 0 ? `👤 说话人 ${line.speaker}` : "👤 未知"}
        </span>

        <div className="subtitle-header-right-meta">
          <span className="subtitle-time-badge">
            {line.start} → {line.end || line.start}
          </span>
          <button
            type="button"
            className={`subtitle-star-btn ${isStarred ? "starred" : ""}`}
            onClick={onToggleStar}
            title={isStarred ? "取消星标" : "标为重点发言"}
          >
            {isStarred ? "⭐" : "✩"}
          </button>
        </div>
      </div>

      <p className="subtitle-line-text" onDoubleClick={handleCopySingle} title="双击复制此行">
        {highlightText(line.text, query)}
      </p>

      {line.translation && (
        <p className="subtitle-translation-text">
          {highlightText(line.translation, query)}
        </p>
      )}
    </div>
  );
}

function escapeRegExp(string: string) {
  return string.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}