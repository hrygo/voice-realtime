import React, { useState } from "react";
import type { UnsavedExchangeItem } from "./innerOSStore";
import { InnerOSAnswerCard } from "./InnerOSAnswerCard";

interface Props {
  readonly items: readonly UnsavedExchangeItem[];
  readonly onSaveItem: (meetingId: string, queryId: string) => Promise<void>;
  readonly onDismissItem: (queryId: string) => void;
  readonly onSelectEvidence?: (segmentId: string) => void;
}

export const InnerOSUnsavedTray: React.FC<Props> = ({
  items,
  onSaveItem,
  onDismissItem,
  onSelectEvidence,
}) => {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [savingIds, setSavingIds] = useState<Set<string>>(new Set());

  const unsavedCount = items.filter((i) => !i.saved).length;
  if (items.length === 0) return null;

  const handleSave = async (meetingId: string, queryId: string) => {
    setSavingIds((prev) => new Set(prev).add(queryId));
    try {
      await onSaveItem(meetingId, queryId);
    } finally {
      setSavingIds((prev) => {
        const next = new Set(prev);
        next.delete(queryId);
        return next;
      });
    }
  };

  return (
    <div className="inner-os-unsaved-tray" data-testid="inner-os-unsaved-tray">
      <div className="inner-os-tray-header">
        <div className="inner-os-tray-title">
          <span className="inner-os-tray-icon">🔒</span>
          <span>会中内心 OS 问答暂存</span>
          {unsavedCount > 0 && (
            <span className="inner-os-tray-badge">{unsavedCount} 条未持久化</span>
          )}
        </div>
        <div className="inner-os-tray-ttl">
          <span>⏳ 服务端暂存有效期 30 分钟</span>
        </div>
      </div>

      <div className="inner-os-tray-list">
        {items.map((item) => {
          const isExpanded = expandedId === item.queryId;
          const isSaving = savingIds.has(item.queryId);

          return (
            <div
              key={item.queryId}
              className={`inner-os-tray-item ${item.saved ? "is-saved" : ""}`}
            >
              <div className="inner-os-tray-item-summary">
                <div
                  className="inner-os-tray-item-q"
                  onClick={() => setExpandedId(isExpanded ? null : item.queryId)}
                >
                  <span className="inner-os-tray-q-icon">Q:</span>
                  <span className="inner-os-tray-q-text">{item.question}</span>
                  <span className="inner-os-tray-expand-hint">
                    {isExpanded ? "▲ 收起" : "▼ 查看回答"}
                  </span>
                </div>
                <div className="inner-os-tray-item-actions">
                  <button
                    type="button"
                    className={`inner-os-tray-save-btn ${item.saved ? "is-saved" : ""}`}
                    onClick={() => handleSave(item.meetingId, item.queryId)}
                    disabled={item.saved || isSaving}
                  >
                    {item.saved ? "✓ 已保存" : isSaving ? "保存中..." : "💾 保存"}
                  </button>
                  <button
                    type="button"
                    className="inner-os-tray-dismiss-btn"
                    onClick={() => onDismissItem(item.queryId)}
                    title="忽略并不保存"
                  >
                    ✕
                  </button>
                </div>
              </div>

              {isExpanded && (
                <div className="inner-os-tray-item-detail">
                  <InnerOSAnswerCard
                    queryId={item.queryId}
                    question={item.question}
                    intent={item.intent}
                    answer={item.answer}
                    saved={item.saved}
                    onSave={() => handleSave(item.meetingId, item.queryId)}
                    onSelectEvidence={onSelectEvidence}
                    isSaving={isSaving}
                  />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};
