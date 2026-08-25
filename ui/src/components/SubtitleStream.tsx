import React, { useState, useEffect, useRef, useMemo, useCallback } from "react";
import { useEventSocket } from "../hooks/useEventSocket";
import {
  formatSpeaker,
  speakerColor,
  toSRT,
  toMarkdownNotes,
  useSubtitleStore,
  type SubtitleLine,
} from "../stores/subtitleStore";
import { useUISettingsStore } from "../stores/uiSettingsStore";
import type { CommandSocketApi } from "../hooks/useCommandSocket";
import { showToast } from "./Toast";
import "./SubtitleStream.css";
import "./ModeSidebar.css";

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

interface SubtitleStreamProps {
  readonly isMeetingRecording?: boolean;
  readonly onNavigateMeeting?: () => void;
  readonly commandSocket?: CommandSocketApi;
}

export default function SubtitleStream({
  isMeetingRecording = false,
  onNavigateMeeting,
  commandSocket,
}: SubtitleStreamProps) {
  const { lines, partial, connected, starredIndices, toggleStar } = useSubtitleStore();
  const teleprompterSettings = useUISettingsStore((s) => s.teleprompterSettings);
  const setTeleprompterSettings = useUISettingsStore((s) => s.setTeleprompterSettings);

  const scrollRef = useRef<HTMLDivElement>(null);
  const presentationScrollRef = useRef<HTMLDivElement>(null);

  const [searchQuery, setSearchQuery] = useState("");
  const [speakerFilter, setSpeakerFilter] = useState<string>("all");
  const [fontSizeMode, setFontSizeMode] = useState<FontSizeMode>("normal");
  const [presentationMode, setPresentationMode] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [showGuideLine, setShowGuideLine] = useState(true);
  const [isScrolledUp, setIsScrolledUp] = useState(false);

  useEffect(() => {
    const handleFullscreenChange = () => {
      setIsFullscreen(!!document.fullscreenElement);
    };
    document.addEventListener("fullscreenchange", handleFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
    } else {
      if (document.exitFullscreen) {
        document.exitFullscreen().catch(() => {});
      }
    }
  }, []);

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

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceToBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setIsScrolledUp(distanceToBottom > 60);
  }, []);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (el) {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
      setIsScrolledUp(false);
    }
    const presEl = presentationScrollRef.current;
    if (presEl) {
      presEl.scrollTo({ top: presEl.scrollHeight, behavior: "smooth" });
    }
  }, []);

  // Auto-scroll (rAF batching to avoid layout thrashing during rapid streaming)
  useEffect(() => {
    if (document.hidden || isScrolledUp) return;
    const frame = requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;

      const presEl = presentationScrollRef.current;
      if (presEl) presEl.scrollTop = presEl.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [lines, partial, isScrolledUp]);

  // Available unique speakers (归一化非负说话人，避免出现 -1)
  const availableSpeakers = useMemo(() => {
    const set = new Set<number>();
    lines.forEach((l) => set.add(l.speaker >= 0 ? l.speaker : 0));
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
          const spk = String(line.speaker >= 0 ? line.speaker : 0);
          if (spk !== speakerFilter) return false;
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
      .map((l) => `[${l.start} - ${l.end}] ${formatSpeaker(l.speaker)}: ${l.text}`)
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
    const content = lines.map((l) => `${formatSpeaker(l.speaker)}: ${l.text}`).join("\n");
    navigator.clipboard.writeText(content).then(
      () => showToast("所有字幕文本已复制到剪贴板", "success"),
      () => showToast("复制失败", "error"),
    );
  }, [lines]);

  const handleClear = useCallback(async () => {
    useSubtitleStore.getState().clear();
    if (commandSocket?.ready) {
      try {
        await commandSocket.sendCommand({ cmd: "clear_subtitles" });
      } catch {
        // Fallback to local clear
      }
    }
    showToast("字幕记录已清空", "info");
  }, [commandSocket]);

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
      } else if (e.key === "Escape" && presentationMode) {
        if (document.fullscreenElement) {
          document.exitFullscreen().catch(() => {});
        }
        setPresentationMode(false);
      }
    };
    window.addEventListener("keydown", handleShortcuts);
    return () => window.removeEventListener("keydown", handleShortcuts);
  }, [handleExportSRT, handleExportMarkdown, presentationMode]);

  return (
    <section
      className="panel subtitle-panel"
      aria-label="实时字幕"
    >
      <div className="subtitle-workspace">
        <aside className="mode-sidebar subtitle-sidebar" aria-label="字幕控制">
          <div className="mode-sidebar-scroll">
            <section className="mode-sidebar-group subtitle-sidebar-group subtitle-sidebar-group-status">
              <div className="mode-sidebar-group-header">
                <span className="mode-sidebar-group-title">
                  <span className="mode-sidebar-group-icon">◉</span>
                  字幕状态
                </span>
                <span className="mode-sidebar-group-meta">实时</span>
              </div>

              <span className={`subtitle-status-pill ${connected ? "connected" : ""}`}>
                <span className="subtitle-status-dot" />
                {connected ? "WhisperLiveKit 已连接" : "等待连接"}
              </span>
              <span
                className="subtitle-mode-pill"
                title="处于实时字幕 Tab 时，AI 语音交互已自动挂起，麦克风仅用于字幕转录"
              >
                <span>🛡️ 纯净字幕</span>
                <small>AI 助手已挂起</small>
              </span>

              {isMeetingRecording && (
                <div
                  className="subtitle-sidebar-sync-card"
                  title="当前会议正在录制中，字幕流与会议声纹分轨保持同步"
                >
                  <div className="subtitle-sidebar-sync-title">
                    <span className="subtitle-sync-dot" />
                    <span>与会议录制同步中</span>
                  </div>
                  {onNavigateMeeting && (
                    <button
                      type="button"
                      className="subtitle-jump-btn"
                      onClick={onNavigateMeeting}
                      title="转到会议助手面板"
                    >
                      查看会议 →
                    </button>
                  )}
                </div>
              )}
            </section>

            <section className="mode-sidebar-group subtitle-sidebar-group subtitle-sidebar-group-display">
              <div className="mode-sidebar-group-header">
                <span className="mode-sidebar-group-title">
                  <span className="mode-sidebar-group-icon">⌕</span>
                  显示与筛选
                </span>
              </div>

              <div className="subtitle-search-wrap">
                <span className="subtitle-search-icon">🔍</span>
                <input
                  id="subtitle-search-input"
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
                    title="清空搜索"
                  >
                    ✕
                  </button>
                )}
              </div>

              <label className="subtitle-sidebar-field-label" htmlFor="subtitle-speaker-select">
                说话人与重点
              </label>
              <select
                id="subtitle-speaker-select"
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

              <div className="subtitle-sidebar-field-label">字幕字号</div>
              <div className="subtitle-font-size-control" role="group" aria-label="调节字幕字号">
                {(
                  [
                    ["normal", "A 小"],
                    ["medium", "A+ 中"],
                    ["large", "A++ 大"],
                  ] as const
                ).map(([size, label]) => (
                  <button
                    key={size}
                    type="button"
                    className={`subtitle-font-size-btn ${fontSizeMode === size ? "active" : ""}`}
                    aria-pressed={fontSizeMode === size}
                    onClick={() => setFontSizeMode(size)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </section>

            <section className="mode-sidebar-group subtitle-sidebar-group subtitle-sidebar-group-actions">
              <div className="mode-sidebar-group-header">
                <span className="mode-sidebar-group-title">
                  <span className="mode-sidebar-group-icon">↗</span>
                  输出与操作
                </span>
              </div>

              <button
                type="button"
                className="presentation-mode-btn subtitle-sidebar-presentation-btn"
                onClick={() => setPresentationMode(true)}
                title="进入舞台提词大字/光学镜像模式 (Cmd+Shift+P)"
              >
                <span>📺</span> 提词大字
                <kbd>⌘⇧P</kbd>
              </button>

              <div className="mode-sidebar-action-grid subtitle-sidebar-actions">
                <button
                  type="button"
                  className="btn-ctrl subtitle-sidebar-action"
                  onClick={handleCopyAll}
                  disabled={!lines.length}
                  title="一键复制全部纯文本字幕"
                >
                  <span>📋</span> 复制
                </button>
                <button
                  type="button"
                  className="btn-ctrl subtitle-sidebar-action"
                  onClick={handleExportMarkdown}
                  disabled={!lines.length}
                  title="导出为结构化 Markdown 会议纪要 (Cmd+Shift+M)"
                >
                  <span>📝</span> 纪要
                </button>
                <button
                  type="button"
                  className="btn-ctrl subtitle-sidebar-action"
                  onClick={handleExportSRT}
                  disabled={!lines.length}
                  title="导出为标准 SRT 字幕文件 (Cmd+Shift+S)"
                >
                  <span>💾</span> SRT
                </button>
                <button
                  type="button"
                  className="btn-ctrl subtitle-sidebar-action"
                  onClick={handleExportTXT}
                  disabled={!lines.length}
                  title="导出为纯文本文件"
                >
                  <span>📄</span> TXT
                </button>
                <button
                  type="button"
                  className="btn-ctrl subtitle-sidebar-action"
                  onClick={handleExportJSON}
                  disabled={!lines.length}
                  title="导出为 JSON 时序数据"
                >
                  <span>📊</span> JSON
                </button>
                <button
                  type="button"
                  className="btn-ctrl btn-ctrl-danger subtitle-sidebar-action"
                  onClick={handleClear}
                  disabled={!lines.length}
                  title="清空当前字幕列表"
                >
                  <span>🗑️</span> 清空
                </button>
              </div>

              <div className="subtitle-sidebar-stats">
                <span>{lines.length} 条字幕</span>
                {starredIndices.size > 0 && <span>⭐ {starredIndices.size} 重点</span>}
              </div>
            </section>
          </div>
        </aside>

        <main className="subtitle-main-stage">
          <header className="panel-header subtitle-header">
            <div className="subtitle-header-left">
              <h2>
                <span>📝</span> 实时字幕
              </h2>
              <span className="subtitle-header-context">
                {isMeetingRecording ? "会议转录同步中" : "本地实时转写工作区"}
              </span>
            </div>
            <div className="subtitle-header-right">
              <span className="subtitle-header-hint">双击字幕可复制 · Cmd+Shift+P 提词</span>
            </div>
          </header>

          <div
            className={`subtitle-stream-body font-${fontSizeMode}`}
            ref={scrollRef}
            onScroll={handleScroll}
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

            {/* 智能贴底悬浮按钮 */}
            {isScrolledUp && (
              <button
                type="button"
                className="subtitle-scroll-bottom-btn"
                onClick={scrollToBottom}
                aria-label="回到底部"
              >
                <span>↓</span> 恢复跟随最新字幕
              </button>
            )}
          </div>

          <footer className="subtitle-bottom-toolbar">
            <div className="subtitle-meta-stats">
              <span>当前显示 {filteredLines.length} / {lines.length} 条字幕</span>
              {starredIndices.size > 0 && <span>· {starredIndices.size} 条重点</span>}
            </div>
            <span className="subtitle-footer-hint">滚动可查看历史，字幕会自动跟随最新内容</span>
          </footer>
        </main>
      </div>

      {/* 演讲/提词器光学镜像大字模式 Modal */}
      {presentationMode && (
        <div
          className="presentation-overlay"
          role="dialog"
          aria-modal="true"
          onClick={() => {
            if (document.fullscreenElement) {
              document.exitFullscreen().catch(() => {});
            }
            setPresentationMode(false);
          }}
        >
          <div className="presentation-header" onClick={(e) => e.stopPropagation()}>
            <div className="presentation-title-wrap">
              <div className="presentation-logo-icon">🎙️</div>
              <div className="presentation-title-text">
                <h2>Voice Studio 舞台提词与大屏</h2>
                <span className="presentation-subtitle-status">
                  {connected ? "● WhisperLiveKit 实时转写" : "○ 等待 ASR 连接"}
                  {" · "}
                  <span>已转录 {lines.length} 条字幕</span>
                </span>
              </div>
            </div>

            <div className="presentation-ctrls">
              <div className="presentation-font-slider-wrap">
                <span className="presentation-ctrl-label">字号</span>
                <input
                  type="range"
                  className="presentation-font-slider"
                  min="1.4"
                  max="3.8"
                  step="0.2"
                  value={teleprompterSettings.fontSize}
                  onChange={(e) =>
                    setTeleprompterSettings({ fontSize: parseFloat(e.target.value) })
                  }
                  title="调节提词字号大小"
                />
                <span className="presentation-font-val">
                  {Number(teleprompterSettings.fontSize).toFixed(1)}rem
                </span>
              </div>

              <button
                type="button"
                className={`presentation-tool-btn ${showGuideLine ? "active" : ""}`}
                onClick={() => setShowGuideLine(!showGuideLine)}
                title="开启/关闭视线阅读导轨"
              >
                <span>📏 导轨 {showGuideLine ? "开" : "关"}</span>
              </button>

              <button
                type="button"
                className="presentation-tool-btn"
                onClick={() =>
                  setTeleprompterSettings({
                    textAlign: teleprompterSettings.textAlign === "center" ? "left" : "center",
                  })
                }
                title="切换居中 / 靠左排版对齐"
              >
                <span>{teleprompterSettings.textAlign === "center" ? "≡ 居中" : "⫷ 居左"}</span>
              </button>

              <button
                type="button"
                className={`presentation-tool-btn ${teleprompterSettings.mirror ? "active" : ""}`}
                onClick={() =>
                  setTeleprompterSettings({ mirror: !teleprompterSettings.mirror })
                }
                title="开启/关闭水平镜像翻转 (支持物理分光镜提词)"
              >
                <span>🪞 {teleprompterSettings.mirror ? "镜像开启" : "光学镜像"}</span>
              </button>

              <button
                type="button"
                className={`presentation-tool-btn ${isFullscreen ? "active" : ""}`}
                onClick={toggleFullscreen}
                title="开启 / 退出全屏投屏"
              >
                <span>{isFullscreen ? "🖵 还原" : "⛶ 全屏"}</span>
              </button>

              <button
                type="button"
                className="presentation-close-btn"
                onClick={() => {
                  if (document.fullscreenElement) {
                    document.exitFullscreen().catch(() => {});
                  }
                  setPresentationMode(false);
                }}
                title="退出大字提词模式 (Esc)"
              >
                退出 (Esc)
              </button>
            </div>
          </div>

          <div
            className={`presentation-body ${teleprompterSettings.mirror ? "mirror-mode" : ""} align-${teleprompterSettings.textAlign || "left"}`}
            ref={presentationScrollRef}
            style={{ fontSize: `${teleprompterSettings.fontSize}rem` }}
            onClick={(e) => e.stopPropagation()}
          >
            {showGuideLine && (
              <div className="teleprompter-guide-line" aria-hidden="true">
                <span className="guide-marker left">▶</span>
                <span className="guide-marker right">◀</span>
              </div>
            )}

            <div className="presentation-container">
              {!lines.length && !partial && (
                <div className="presentation-empty">
                  <span className="presentation-empty-icon">🎙️</span>
                  <h3>舞台提词与字幕大屏已就绪</h3>
                  <p>WhisperLiveKit 正在实时监听中，发言将实时以大字投屏呈现</p>
                </div>
              )}

              {lines.map((line, idx) => {
                const isLatest = idx === lines.length - 1 && !partial;
                return (
                  <div
                    className={`presentation-line ${isLatest ? "latest-line" : ""}`}
                    key={idx}
                  >
                    <span
                      className="presentation-speaker-tag"
                      style={{ color: speakerColor(line.speaker) }}
                    >
                      👤 {formatSpeaker(line.speaker)}
                    </span>
                    <span className="presentation-line-text">{line.text}</span>
                  </div>
                );
              })}

              {partial && (
                <div className="presentation-partial">
                  <span className="presentation-partial-indicator" />
                  <span className="presentation-partial-text">{partial}</span>
                </div>
              )}
            </div>
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
    navigator.clipboard.writeText(`[${line.start}] ${formatSpeaker(line.speaker)}: ${line.text}`).then(
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
          👤 {formatSpeaker(line.speaker)}
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
