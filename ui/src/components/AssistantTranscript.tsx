import type { RefObject } from "react";

import type { AssistantBubble } from "../stores/assistantStore";
import { MarkdownRenderer } from "./meeting/MarkdownRenderer";

interface AssistantTranscriptProps {
  readonly transcript: readonly AssistantBubble[];
  readonly scrollRef: RefObject<HTMLDivElement | null>;
  readonly isScrolledUp: boolean;
  readonly copiedKey: string | null;
  readonly playingBubbleKey: string | null;
  readonly textInput: string;
  readonly duplexSummary: string;
  readonly onScroll: () => void;
  readonly onScrollToBottom: () => void;
  readonly onReplay: (text: string, key: string) => Promise<void>;
  readonly onCopy: (text: string, key: string) => void;
  readonly onTextInputChange: (value: string) => void;
  readonly onSendText: () => Promise<void>;
}

export function AssistantTranscript({
  transcript,
  scrollRef,
  isScrolledUp,
  copiedKey,
  playingBubbleKey,
  textInput,
  duplexSummary,
  onScroll,
  onScrollToBottom,
  onReplay,
  onCopy,
  onTextInputChange,
  onSendText,
}: AssistantTranscriptProps) {
  return (
    <div className="assistant-transcript-container">
      <div
        className="assistant-transcript"
        ref={scrollRef}
        onScroll={onScroll}
        aria-live="polite"
      >
        {transcript.map((bubble, index) => {
          const bubbleKey = `${bubble.role}-${bubble.turnId ?? index}-${bubble.timestamp ?? index}`;
          const isCopied = copiedKey === bubbleKey;
          return (
            <div className={`assistant-bubble-row ${bubble.role}`} key={bubbleKey}>
              <div className="bubble-meta-header">
                <span className="bubble-role-badge">
                  {bubble.role === "user" ? "👤 你" : "🤖 AI 助手"}
                </span>
                {bubble.turnId !== undefined && (
                  <span className="bubble-turn-pill">#{bubble.turnId}</span>
                )}
                {bubble.timestamp && (
                  <span className="bubble-time-pill">{bubble.timestamp}</span>
                )}
                {bubble.interrupted && (
                  <span className="bubble-interrupted-tag">⚡ 已打断 (耳机插话)</span>
                )}
              </div>
              <div className={`bubble-card ${bubble.final ? "final" : "streaming"}`}>
                {bubble.role === "assistant"
                  ? <MarkdownRenderer content={bubble.text} />
                  : <span>{bubble.text}</span>}
                <div className="bubble-actions-group">
                  {bubble.role === "assistant" && bubble.final && (
                    <button
                      type="button"
                      className={`bubble-action-btn ${playingBubbleKey === bubbleKey ? "playing" : ""}`}
                      onClick={() => void onReplay(bubble.text, bubbleKey)}
                      disabled={playingBubbleKey !== null}
                      title="使用当前音色重新朗读此条回复"
                    >
                      {playingBubbleKey === bubbleKey ? "🔊 播报中..." : "🔊 朗读"}
                    </button>
                  )}
                  <button
                    type="button"
                    className={`bubble-action-btn ${isCopied ? "copied" : ""}`}
                    onClick={() => onCopy(bubble.text, bubbleKey)}
                    title="复制内容"
                  >
                    {isCopied ? "✓ 已复制" : "📋 复制"}
                  </button>
                </div>
              </div>
            </div>
          );
        })}

        {!transcript.length && (
          <div className="assistant-empty-state">
            <span className="empty-state-icon">🎙️</span>
            <p className="empty-state-title">等待语音输入...</p>
            <p className="empty-state-desc">
              直接对着麦克风说话，AI 助手将实时转写、推理并语音应答。{duplexSummary}。
            </p>
          </div>
        )}
      </div>

      {isScrolledUp && (
        <button
          type="button"
          className="scroll-to-bottom-btn"
          onClick={onScrollToBottom}
          aria-label="回到底部"
        >
          <span>↓</span> 最新对话
        </button>
      )}

      <div className="assistant-input-bar">
        <input
          type="text"
          className="assistant-text-input"
          placeholder="💬 输入文字与助手对话 (按 Enter 发送)..."
          value={textInput}
          onChange={(event) => onTextInputChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void onSendText();
            }
          }}
        />
        <button
          type="button"
          className="assistant-send-btn"
          onClick={() => void onSendText()}
          disabled={!textInput.trim()}
          title="发送文字消息"
        >
          <span>↑</span> 发送
        </button>
      </div>
    </div>
  );
}
