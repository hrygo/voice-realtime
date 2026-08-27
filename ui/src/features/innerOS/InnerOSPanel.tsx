import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { useMeetingStore } from "../../stores/meetingStore";
import { useInnerOSStore } from "./innerOSStore";
import { useInnerOSSocket } from "./useInnerOSSocket";
import { InnerOSEphemeralContextDrawer } from "./InnerOSEphemeralContext";
import { InnerOSQuickPills } from "./InnerOSQuickPills";
import { InnerOSAnswerCard } from "./InnerOSAnswerCard";
import type {
  InnerOSEphemeralContext,
  InnerOSIntent,
} from "./contracts";
import "./InnerOSPanel.css";

interface Props {
  readonly onSelectEvidence?: (segmentId: string) => void;
}

export const InnerOSPanel: React.FC<Props> = ({ onSelectEvidence }) => {
  const activeMeetingId = useMeetingStore((s) => s.activeMeetingId);
  const status = useMeetingStore((s) => s.status);
  const transcriptRevision = useMeetingStore((s) => s.transcriptRevision);
  const starredMap = useMeetingStore((s) => s.starredMap);

  const starredSet = activeMeetingId ? starredMap[activeMeetingId] : undefined;
  const starredIds = useMemo(() => (starredSet ? Array.from(starredSet) : []), [starredSet]);

  const isPanelOpen = useInnerOSStore((s) => s.isPanelOpen);
  const togglePanel = useInnerOSStore((s) => s.togglePanel);
  const activeQuestion = useInnerOSStore((s) => s.activeQuestion);
  const activeIntent = useInnerOSStore((s) => s.activeIntent);
  const activeQueryId = useInnerOSStore((s) => s.activeQueryId);
  const queryStatus = useInnerOSStore((s) => s.queryStatus);
  const activeAnswer = useInnerOSStore((s) => s.activeAnswer);
  const activeAnswerSaved = useInnerOSStore((s) => s.activeAnswerSaved);
  const activeError = useInnerOSStore((s) => s.activeError);
  const saveExchangeAction = useInnerOSStore((s) => s.saveExchangeAction);
  const clearActiveQuery = useInnerOSStore((s) => s.clearActiveQuery);

  // Ephemeral context (strictly local component state, reset on unmount/meeting switch)
  const [ephemeralContext, setEphemeralContext] = useState<InnerOSEphemeralContext>({});
  const [contextVersion, setContextVersion] = useState<number>(1);

  // Focus segment state
  const [isFocusActive, setIsFocusActive] = useState(true);

  // Input & Intent
  const [inputText, setInputText] = useState("");
  const [selectedIntent, setSelectedIntent] = useState<InnerOSIntent>("mixed");
  const [isSaving, setIsSaving] = useState(false);

  const inputRef = useRef<HTMLTextAreaElement>(null);
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

      sendQuery(q, intent, transcriptRevision, ephemeralPayload, focusIds);
      if (!customQuestion) {
        setInputText("");
      }
    },
    [
      inputText,
      selectedIntent,
      activeMeetingId,
      isConnected,
      isFocusActive,
      starredIds,
      ephemeralContext,
      transcriptRevision,
      sendQuery,
    ],
  );

  const handleSaveActiveAnswer = async () => {
    if (!activeMeetingId || !activeQueryId) return;
    setIsSaving(true);
    try {
      await saveExchangeAction(activeMeetingId, activeQueryId);
    } finally {
      setIsSaving(false);
    }
  };

  // Keyboard shortcut listener for panel: ⌘+K to focus input, Esc to cancel
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (!isPanelOpen) {
          useInnerOSStore.getState().openPanel();
        }
        inputRef.current?.focus();
      } else if (e.key === "Escape") {
        if (queryStatus === "generating" || queryStatus === "accepted") {
          e.preventDefault();
          sendCancel();
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isPanelOpen, queryStatus, sendCancel]);

  if (!isPanelOpen) {
    return null;
  }

  const isGenerating = queryStatus === "generating" || queryStatus === "accepted";

  return (
    <aside className="inner-os-panel" data-testid="inner-os-panel">
      {/* Top Header */}
      <div className="inner-os-header">
        <div className="inner-os-title-wrap">
          <div className="inner-os-shield-icon">🔒</div>
          <div className="inner-os-title-text">
            <h3>内心 OS · 私密副驾驶</h3>
            <span className="inner-os-subtitle">
              {status !== "recording"
                ? "⏸️ 会议待命中 · 仅你可见"
                : isConnected
                  ? "🟢 专用通道已就绪 · 仅你可见"
                  : "🟡 正在建立私密连接..."}
            </span>
          </div>
        </div>
        <div className="inner-os-header-actions">
          <button
            type="button"
            className="inner-os-collapse-btn"
            onClick={togglePanel}
            title="收起内心 OS 面板 (⌘+K)"
          >
            ✕
          </button>
        </div>
      </div>

      {/* Fail-closed security warning if not loopback */}
      {!isLoopbackSecure && (
        <div className="inner-os-security-alert">
          <span className="inner-os-alert-icon">⚠️</span>
          <span>局域网访问模式：内心 OS 已按单机私密安全规范 fail-closed。请通过本机 127.0.0.1 访问。</span>
        </div>
      )}

      <div className="inner-os-content-scroll">
        {/* Ephemeral Context (Goal / Agenda / Background) */}
        <InnerOSEphemeralContextDrawer
          context={ephemeralContext}
          version={contextVersion}
          onChange={handleContextChange}
          onClear={handleClearContext}
        />

        {/* Starred Segments & Quick Query Pills */}
        <InnerOSQuickPills
          starredCount={starredIds.length}
          isFocusActive={isFocusActive}
          onToggleFocus={() => setIsFocusActive(!isFocusActive)}
          onSelectQuickQuery={(q, intent) => handleSubmit(q, intent)}
          disabled={!isConnected || isGenerating}
        />

        {/* Active Query Status & Answer Stream */}
        <div className="inner-os-stream-container">
          {isGenerating && (
            <div className="inner-os-generating-box" data-testid="inner-os-generating">
              <div className="inner-os-generating-header">
                <span className="inner-os-pulsing-dot" />
                <span className="inner-os-generating-title">
                  {queryStatus === "accepted" ? "已受理，等待模型算力..." : "正在深入研判会议上下文..."}
                </span>
                <button
                  type="button"
                  className="inner-os-cancel-btn"
                  onClick={sendCancel}
                  title="取消本次研判 (Esc)"
                >
                  ✕ 取消
                </button>
              </div>
              <div className="inner-os-generating-question">
                <strong>Q:</strong> {activeQuestion}
              </div>
            </div>
          )}

          {queryStatus === "failed" && activeError && (
            <div className="inner-os-error-box" data-testid="inner-os-error">
              <div className="inner-os-error-icon">⚠️</div>
              <div className="inner-os-error-msg">
                <strong>研判未完成:</strong> {activeError.message}
              </div>
              <button
                type="button"
                className="inner-os-retry-btn"
                onClick={() => handleSubmit(activeQuestion || "", activeIntent || "mixed")}
              >
                🔄 重试
              </button>
            </div>
          )}

          {queryStatus === "completed" && activeAnswer && activeQueryId && (
            <InnerOSAnswerCard
              queryId={activeQueryId}
              question={activeQuestion || ""}
              intent={activeIntent || "mixed"}
              answer={activeAnswer}
              saved={activeAnswerSaved}
              onSave={handleSaveActiveAnswer}
              onFollowUp={(q) => {
                setInputText(q);
                inputRef.current?.focus();
              }}
              onSelectEvidence={onSelectEvidence}
              isSaving={isSaving}
            />
          )}

          {queryStatus === "idle" && (
            <div className="inner-os-placeholder-guide">
              <div className="inner-os-guide-card">
                <span className="inner-os-guide-icon">💡</span>
                <p>实时向内心 OS 提问，快速核查事实、评估判断并草拟回应，全程不出声、不扰会、不入群。</p>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Bottom Input Dock */}
      <div className="inner-os-dock">
        <div className="inner-os-dock-controls">
          <div className="inner-os-intent-selector">
            <label htmlFor="inner-os-intent-select">研判意图:</label>
            <select
              id="inner-os-intent-select"
              value={selectedIntent}
              onChange={(e) => setSelectedIntent(e.target.value as InnerOSIntent)}
              disabled={!isConnected || isGenerating}
            >
              <option value="mixed">⚡ 综合研判 (默认)</option>
              <option value="fact">📋 事实核查</option>
              <option value="analysis">🧠 局势评估</option>
              <option value="draft">✍️ 回应草稿</option>
            </select>
          </div>
          <span className="inner-os-shortcut-hint">⌘+Enter 发送 · Esc 取消</span>
        </div>

        <div className="inner-os-dock-input-wrap">
          <textarea
            ref={inputRef}
            rows={2}
            className="inner-os-textarea"
            placeholder={
              status !== "recording"
                ? "请先开始会议录制以启用内心 OS..."
                : "向内心 OS 提问... (例如：刚才张总提到的性能指标是什么？)"
            }
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                handleSubmit();
              }
            }}
            disabled={!isConnected || isGenerating}
          />
          <button
            type="button"
            className="inner-os-submit-btn"
            onClick={() => handleSubmit()}
            disabled={!inputText.trim() || !isConnected || isGenerating}
            title="发送提问 (⌘+Enter)"
          >
            {isGenerating ? "⏳" : "➔"}
          </button>
        </div>
      </div>
    </aside>
  );
};
