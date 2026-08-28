import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { createPortal } from "react-dom";
import { useMeetingStore } from "../../stores/meetingStore";
import { useInnerOSStore } from "./innerOSStore";
import { useInnerOSSocket } from "./useInnerOSSocket";
import { InnerOSEphemeralContextDrawer } from "./InnerOSEphemeralContext";
import { InnerOSQuickPills } from "./InnerOSQuickPills";
import { InnerOSAnswerCard } from "./InnerOSAnswerCard";
import {
  CheckIcon,
  DownloadIcon,
  EditIcon,
  ExternalLinkIcon,
  MaskIcon,
  RefreshCwIcon,
  SendIcon,
  SparklesIcon,
  XIcon,
  ZapIcon,
} from "../../components/Icons";
import type {
  InnerOSEphemeralContext,
  InnerOSIntent,
  QuickPromptCategory,
} from "./contracts";
import "./InnerOSPanel.css";

interface Props {
  readonly onSelectEvidence?: (segmentId: string) => void;
  readonly isStandalone?: boolean;
}

export const InnerOSPanel: React.FC<Props> = ({ onSelectEvidence, isStandalone = false }) => {
  const activeMeetingId = useMeetingStore((s) => s.activeMeetingId);
  const status = useMeetingStore((s) => s.status);
  const starredMap = useMeetingStore((s) => s.starredMap);

  const starredSet = activeMeetingId ? starredMap[activeMeetingId] : undefined;
  const starredIds = useMemo(() => (starredSet ? Array.from(starredSet) : []), [starredSet]);

  const isPanelOpen = useInnerOSStore((s) => s.isPanelOpen);
  const togglePanel = useInnerOSStore((s) => s.togglePanel);
  const activeQuestion = useInnerOSStore((s) => s.activeQuestion);
  const activeIntent = useInnerOSStore((s) => s.activeIntent);
  const queryStatus = useInnerOSStore((s) => s.queryStatus);
  const activeAnswer = useInnerOSStore((s) => s.activeAnswer);
  const activeError = useInnerOSStore((s) => s.activeError);
  const unsavedExchanges = useInnerOSStore((s) => s.unsavedExchanges);
  const questionHistory = useInnerOSStore((s) => s.questionHistory);
  const saveExchangeAction = useInnerOSStore((s) => s.saveExchangeAction);
  const saveAllExchangesAction = useInnerOSStore((s) => s.saveAllExchangesAction);
  const deleteExchangeAction = useInnerOSStore((s) => s.deleteExchangeAction);
  const dismissUnsavedItem = useInnerOSStore((s) => s.dismissUnsavedItem);
  const exportNotesAsMarkdown = useInnerOSStore((s) => s.exportNotesAsMarkdown);
  const clearActiveQuery = useInnerOSStore((s) => s.clearActiveQuery);

  // Ephemeral context (strictly local component state, reset on unmount/meeting switch)
  const [ephemeralContext, setEphemeralContext] = useState<InnerOSEphemeralContext>({});
  const [contextVersion, setContextVersion] = useState<number>(1);

  // Focus segment state
  const [isFocusActive, setIsFocusActive] = useState(true);

  // Input & Intent & Quick Prompt Category Synchronization
  const [inputText, setInputText] = useState("");
  const [selectedIntent, setSelectedIntent] = useState<InnerOSIntent>("mixed");
  const [activePromptCategory, setActivePromptCategory] = useState<QuickPromptCategory>("fact");
  const [isSavingAll, setIsSavingAll] = useState(false);
  const [isInputFocused, setIsInputFocused] = useState(false);
  const [historyNavIdx, setHistoryNavIdx] = useState<number>(-1);
  const [copiedNotes, setCopiedNotes] = useState(false);

  // Generating elapsed timer (e.g. ⏱️ 2.4s)
  const [elapsedSecs, setElapsedSecs] = useState(0);

  const inputRef = useRef<HTMLTextAreaElement>(null);
  const streamBottomRef = useRef<HTMLDivElement>(null);
  const prevMeetingIdRef = useRef(activeMeetingId);

  const {
    isConnected,
    isLoopbackSecure,
    sendQuery,
    sendCancel,
  } = useInnerOSSocket({
    meetingId: activeMeetingId,
    enabled: isPanelOpen && status === "recording",
  });

  const isGenerating = queryStatus === "generating" || queryStatus === "accepted";

  // Live timer for generating state
  useEffect(() => {
    let interval: ReturnType<typeof setInterval> | undefined;
    if (isGenerating) {
      const startTime = Date.now();
      setElapsedSecs(0);
      interval = setInterval(() => {
        setElapsedSecs((Date.now() - startTime) / 1000);
      }, 100);
    } else {
      setElapsedSecs(0);
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [isGenerating]);

  // Auto-scroll stream when a query finishes or starts
  useEffect(() => {
    if (typeof streamBottomRef.current?.scrollIntoView === "function") {
      streamBottomRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [unsavedExchanges.length, isGenerating]);

  // Reset ephemeral context when meeting changes
  useEffect(() => {
    if (prevMeetingIdRef.current !== activeMeetingId) {
      prevMeetingIdRef.current = activeMeetingId;
      setEphemeralContext({});
      setContextVersion(1);
      clearActiveQuery();
    }
  }, [activeMeetingId, clearActiveQuery]);

  const handleContextChange = (nextCtx: InnerOSEphemeralContext) => {
    setEphemeralContext(nextCtx);
    setContextVersion((v) => v + 1);
  };

  const handleClearContext = () => {
    setEphemeralContext({});
    setContextVersion((v) => v + 1);
  };

  const handleSubmit = useCallback(
    (customQuestion?: string, customIntent?: InnerOSIntent) => {
      const q = (customQuestion || inputText).trim();
      const intent = customIntent || selectedIntent;
      if (!q || !activeMeetingId || !isConnected) return;

      const focusIds =
        isFocusActive && starredIds.length > 0 ? starredIds : undefined;

      const ephemeralPayload =
        ephemeralContext.goal || ephemeralContext.agenda || ephemeralContext.background
          ? ephemeralContext
          : null;

      sendQuery(q, intent, contextVersion, ephemeralPayload, focusIds);
      if (!customQuestion) {
        setInputText("");
      }
      setHistoryNavIdx(-1);
    },
    [
      inputText,
      selectedIntent,
      activeMeetingId,
      isConnected,
      isFocusActive,
      starredIds,
      ephemeralContext,
      contextVersion,
      sendQuery,
    ],
  );

  const handleSaveItem = async (meetingId: string, exchangeId: string) => {
    if (!meetingId || !exchangeId) return;
    await saveExchangeAction(meetingId, exchangeId);
  };

  const handleDeleteItem = async (meetingId: string, queryId: string, saved: boolean) => {
    if (saved && meetingId) {
      try {
        await deleteExchangeAction(meetingId, queryId);
      } catch (err) {
        console.error("Failed to delete saved exchange", err);
      }
    } else {
      dismissUnsavedItem(queryId);
    }
  };

  const handleSaveAll = async () => {
    if (!activeMeetingId) return;
    setIsSavingAll(true);
    try {
      await saveAllExchangesAction(activeMeetingId);
    } finally {
      setIsSavingAll(false);
    }
  };

  const handleExportNotes = async () => {
    const md = exportNotesAsMarkdown();
    try {
      await navigator.clipboard.writeText(md);
      setCopiedNotes(true);
      setTimeout(() => setCopiedNotes(false), 2500);
    } catch {
      // fallback
    }
  };

  // Click outside to dismiss floating drawer
  useEffect(() => {
    if (!isPanelOpen || isStandalone) return;

    const handlePointerDownOutside = (e: MouseEvent | PointerEvent | TouchEvent) => {
      const target = e.target as Node | null;
      if (!target) return;

      // Do nothing if clicked inside the Inner OS panel itself
      const panelEl = document.querySelector('[data-testid="inner-os-panel"]');
      if (panelEl && panelEl.contains(target)) {
        return;
      }

      // Do nothing if clicking on toolbar trigger or floating widget toggle
      if (
        target instanceof Element &&
        (target.closest(".btn-inneros-toggle") ||
          target.closest(".inner-os-floating-trigger") ||
          target.closest(".inner-os-panel"))
      ) {
        return;
      }

      // Outside click detected -> close panel
      useInnerOSStore.getState().closePanel();
    };

    const timer = setTimeout(() => {
      document.addEventListener("pointerdown", handlePointerDownOutside, true);
    }, 10);

    return () => {
      clearTimeout(timer);
      document.removeEventListener("pointerdown", handlePointerDownOutside, true);
    };
  }, [isPanelOpen, isStandalone]);

  // Keyboard shortcut listener: Esc, Up/Down arrow history, ⌘+Shift+C
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // ⌘ + Shift + C: copy latest draft
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === "c" || e.key === "C")) {
        const latestDraft = activeAnswer?.draft?.text || unsavedExchanges[0]?.answer?.draft?.text;
        if (latestDraft) {
          e.preventDefault();
          navigator.clipboard.writeText(latestDraft);
        }
        return;
      }

      if (e.key === "Escape") {
        if (queryStatus === "generating" || queryStatus === "accepted") {
          e.preventDefault();
          sendCancel();
        } else if (isPanelOpen) {
          e.preventDefault();
          useInnerOSStore.getState().closePanel();
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isPanelOpen, queryStatus, sendCancel, activeAnswer, unsavedExchanges]);

  // Terminal-style history traversal with ArrowUp and ArrowDown
  const handleInputKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      handleSubmit();
      return;
    }

    if (e.key === "ArrowUp" && (inputText === "" || inputRef.current?.selectionStart === 0)) {
      if (questionHistory.length > 0) {
        e.preventDefault();
        const nextIdx = Math.min(historyNavIdx + 1, questionHistory.length - 1);
        setHistoryNavIdx(nextIdx);
        setInputText(questionHistory[nextIdx]);
      }
    } else if (e.key === "ArrowDown" && historyNavIdx >= 0) {
      e.preventDefault();
      const nextIdx = historyNavIdx - 1;
      setHistoryNavIdx(nextIdx);
      if (nextIdx < 0) {
        setInputText("");
      } else {
        setInputText(questionHistory[nextIdx]);
      }
    }
  };

  // Auto-focus input when panel opens
  useEffect(() => {
    if (isPanelOpen) {
      const timer = setTimeout(() => {
        inputRef.current?.focus();
      }, 60);
      return () => clearTimeout(timer);
    }
  }, [isPanelOpen]);

  const handleOpenInNewTab = useCallback(() => {
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("view", "inner-os");
      window.open(url.toString(), "_blank", "noopener,noreferrer");
    } catch {
      window.open("?view=inner-os", "_blank");
    }
  }, []);

  if (!isStandalone && !isPanelOpen) {
    return null;
  }

  const unsavedCount = unsavedExchanges.filter((item) => !item.saved).length;

  const content = (
    <aside
      className={`inner-os-panel ${isStandalone ? "is-standalone" : ""}`}
      data-testid="inner-os-panel"
      data-standalone={isStandalone ? "true" : "false"}
    >
      {/* Top Header */}
      <div className="inner-os-header">
        <div className="inner-os-title-wrap">
          <div className="inner-os-shield-icon" aria-hidden="true">
            <MaskIcon size={18} />
          </div>
          <div className="inner-os-title-text">
            <div className="inner-os-title-main">
              <h3>内心 OS · 私密副驾驶</h3>
            </div>
            <span className="inner-os-subtitle" role="status" aria-live="polite">
              {status !== "recording"
                ? "会议待命中"
                : isConnected
                  ? "通道已就绪 · 端侧大模型推理"
                  : "正在建立本地私密连接..."}
            </span>
          </div>
        </div>

        {/* Header Action Tools */}
        <div className="inner-os-header-tools">
          <button
            type="button"
            className="inner-os-tool-btn"
            onClick={handleExportNotes}
            title="导出整场私密笔记为 Markdown 到剪贴板"
            aria-label="导出整场私密笔记"
          >
            {copiedNotes ? <><CheckIcon size={12} /> 已复制笔记</> : <><DownloadIcon size={12} /> 导出笔记</>}
          </button>

          {unsavedCount > 1 && (
            <button
              type="button"
              className="inner-os-tool-btn is-save-all"
              onClick={handleSaveAll}
              disabled={isSavingAll}
              title="一键将本场全部临时问答归档至会议数据库"
            >
              {isSavingAll ? "保存中..." : `保存全部 (${unsavedCount})`}
            </button>
          )}

          {!isStandalone && (
            <button
              type="button"
              className="inner-os-tool-btn"
              onClick={handleOpenInNewTab}
              title="在独立浏览器标签页中打开内心 OS"
              aria-label="在独立标签页打开内心 OS"
            >
              <ExternalLinkIcon size={13} />
              <span>独立窗口</span>
            </button>
          )}

          {!isStandalone && (
            <button
              type="button"
              className="inner-os-close-btn"
              onClick={togglePanel}
              title="收起内心 OS (Esc / ⌘K)"
              aria-label="收起内心 OS 面板"
            >
              <XIcon size={15} />
            </button>
          )}
        </div>
      </div>

      {/* Fail-closed security warning if not loopback */}
      {!isLoopbackSecure && (
        <div className="inner-os-security-alert" role="alert">
          <span className="inner-os-alert-icon" aria-hidden="true">!</span>
          <span>局域网访问模式：内心 OS 已按单机私密安全规范 fail-closed。请通过本机 127.0.0.1 访问。</span>
        </div>
      )}

      <div className="inner-os-content-scroll">
        {/* Ephemeral Context (Goal / Agenda / Background) */}
        <InnerOSEphemeralContextDrawer
          context={ephemeralContext}
          onChange={handleContextChange}
          onClear={handleClearContext}
        />

        {/* Starred Segments & Categorized Quick Prompt Matrix */}
        <InnerOSQuickPills
          starredCount={starredIds.length}
          isFocusActive={isFocusActive}
          onToggleFocus={() => setIsFocusActive(!isFocusActive)}
          onSelectQuickQuery={(q, intent) => handleSubmit(q, intent)}
          activeCategory={activePromptCategory}
          onCategoryChange={(cat) => {
            setActivePromptCategory(cat);
            if (cat === "fact" || cat === "analysis" || cat === "draft") {
              setSelectedIntent(cat);
            }
          }}
          disabled={!isConnected || isGenerating}
        />

        {/* Multi-Turn Thought Stream Container */}
        <div className="inner-os-stream-container">
          {/* Render past session exchanges from latest to oldest or in chronological order */}
          {unsavedExchanges.map((exchange, index) => {
            const isLatest = index === 0;
            return (
              <InnerOSAnswerCard
                key={exchange.queryId}
                queryId={exchange.queryId}
                question={exchange.question}
                intent={exchange.intent}
                answer={exchange.answer}
                saved={exchange.saved}
                createdAt={exchange.createdAt}
                isCollapsible={unsavedExchanges.length > 1}
                defaultExpanded={isLatest}
                onSave={() => handleSaveItem(exchange.meetingId, exchange.queryId)}
                onDelete={() => handleDeleteItem(exchange.meetingId, exchange.queryId, exchange.saved)}
                onFollowUp={(q) => {
                  setInputText(q);
                  inputRef.current?.focus();
                }}
                onSelectEvidence={onSelectEvidence}
              />
            );
          })}

          {/* Active Generating Shimmer Box */}
          {isGenerating && (
            <div
              className="inner-os-generating-box"
              data-testid="inner-os-generating"
              role="status"
              aria-live="polite"
            >
              <div className="inner-os-generating-header">
                <div className="inner-os-generating-pulse-indicator">
                  <span className="inner-os-pulsing-dot" />
                  <span className="inner-os-generating-timer">
                    {elapsedSecs.toFixed(1)}s
                  </span>
                </div>
                <span className="inner-os-generating-title">
                  {queryStatus === "accepted"
                    ? "已受理，调配本地推理模型..."
                    : "正在深入研判会议上下文与事实..."}
                </span>
                <button
                  type="button"
                  className="inner-os-cancel-btn"
                  onClick={sendCancel}
                  title="取消本次研判 (Esc)"
                >
                  <XIcon size={12} /> 取消
                </button>
              </div>

              <div className="inner-os-generating-question">
                <strong>Q:</strong> {activeQuestion}
              </div>

              {/* Progressive skeleton bars */}
              <div className="inner-os-generating-skeleton">
                <div className="inner-os-skeleton-bar bar-1" />
                <div className="inner-os-skeleton-bar bar-2" />
                <div className="inner-os-skeleton-bar bar-3" />
              </div>
            </div>
          )}

          {/* Query Failure Error Box */}
          {queryStatus === "failed" && activeError && (
            <div className="inner-os-error-box" data-testid="inner-os-error" role="alert">
              <div className="inner-os-error-icon" aria-hidden="true">!</div>
              <div className="inner-os-error-msg">
                <strong>研判未完成:</strong> {activeError.message}
              </div>
              <button
                type="button"
                className="inner-os-retry-btn"
                onClick={() => handleSubmit(activeQuestion || "", activeIntent || "mixed")}
              >
                <RefreshCwIcon size={13} /> 重试
              </button>
            </div>
          )}

          {/* Empty stream placeholder guide */}
          {unsavedExchanges.length === 0 && !isGenerating && queryStatus !== "failed" && (
            <div className="inner-os-placeholder-guide">
              <div className="inner-os-guide-card">
                <span className="inner-os-guide-icon" aria-hidden="true">
                  <SparklesIcon size={22} />
                </span>
                <h4>{queryStatus === "cancelled" ? "研判已终止" : "会场私密智囊 · 沉着运筹于心"}</h4>
                <p>
                  {queryStatus === "cancelled"
                    ? "本次研判已取消，可继续提问。"
                    : "实时捕捉未明之意，求证关键共识，在重要关头为你提供有分寸、有力量的发言洞察。全程离线单机、无声不扰会。"}
                </p>
                <div className="inner-os-guide-tips">
                  <div className="inner-os-tip-item">
                    <SparklesIcon size={13} />
                    <span>快捷指令：快速拆解会场局势与核心分歧</span>
                  </div>
                  <div className="inner-os-tip-item">
                    <ZapIcon size={13} />
                    <span>重点聚焦：在转录流按 <kbd>S</kbd> 标记关键发言，加权引导深度研判</span>
                  </div>
                  <div className="inner-os-tip-item">
                    <EditIcon size={13} />
                    <span>快捷提取：按 <kbd>⌘ + Shift + C</kbd> 一键提取发言草稿</span>
                  </div>
                </div>
              </div>
            </div>
          )}

          <div ref={streamBottomRef} />
        </div>
      </div>

      {/* Bottom Input Dock */}
      <div className="inner-os-dock">
        <div className="inner-os-dock-controls">
          <div className="inner-os-intent-selector">
            <label htmlFor="inner-os-intent-select">意图:</label>
            <select
              id="inner-os-intent-select"
              value={selectedIntent}
              onChange={(e) => {
                const nextIntent = e.target.value as InnerOSIntent;
                setSelectedIntent(nextIntent);
                if (nextIntent === "fact" || nextIntent === "analysis" || nextIntent === "draft") {
                  setActivePromptCategory(nextIntent);
                }
              }}
              disabled={!isConnected || isGenerating}
            >
              <option value="mixed">综合研判 (默认)</option>
              <option value="fact">事实核查</option>
              <option value="analysis">局势研判</option>
              <option value="draft">回应草稿</option>
            </select>
          </div>
          {(isInputFocused || isGenerating) && (
            <span className="inner-os-shortcut-hint" id="inner-os-shortcut-hint">
              ⌘+Enter 发送 · ↑↓ 历史{isGenerating ? " · Esc 取消" : ""}
            </span>
          )}
        </div>

        <div className="inner-os-dock-input-wrap">
          <textarea
            ref={inputRef}
            rows={2}
            className="inner-os-textarea"
            aria-label="向内心 OS 提问"
            aria-describedby={isInputFocused || isGenerating ? "inner-os-shortcut-hint" : undefined}
            placeholder={
              status !== "recording"
                ? "会议进行中可随时向内心 OS 提问，全程单机离线研判..."
                : selectedIntent === "fact"
                  ? "核查事实依据... 如：刚才各方达成的具体指标、排期承诺或责任归属"
                  : selectedIntent === "analysis"
                    ? "研判博弈局势... 如：探寻对方未明言的核心关切、潜在顾虑与底线"
                    : selectedIntent === "draft"
                      ? "草拟发言对策... 如：帮我草拟一段得体推动共识、委婉拒绝或有力回击的发言"
                      : "洞悉会场局势、求证关键分歧、草拟发言建议... (⌘+Enter 发送)"
            }
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onFocus={() => setIsInputFocused(true)}
            onBlur={() => setIsInputFocused(false)}
            onKeyDown={handleInputKeyDown}
            disabled={!isConnected || isGenerating}
          />
          <button
            type="button"
            className="inner-os-submit-btn"
            onClick={() => handleSubmit()}
            disabled={!inputText.trim() || !isConnected || isGenerating}
            title="发送提问 (⌘+Enter)"
            aria-label={isGenerating ? "正在生成回答" : "发送提问"}
            aria-busy={isGenerating}
          >
            {isGenerating ? "…" : <SendIcon size={18} />}
          </button>
        </div>
      </div>
    </aside>
  );

  if (isStandalone || typeof document === "undefined") {
    return content;
  }

  return createPortal(content, document.body);
};
