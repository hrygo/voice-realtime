import React, { useState } from "react";
import type { UnsavedExchangeItem } from "./innerOSStore";
import { InnerOSAnswerContent } from "./InnerOSAnswerContent";
import "./InnerOSArchive.css";
import {
  CheckIcon,
  ChevronRightIcon,
  DownloadIcon,
  MaskIcon,
  TrashIcon,
} from "../../components/Icons";

interface Props {
  readonly items: readonly UnsavedExchangeItem[];
  readonly onSaveItem: (meetingId: string, queryId: string) => Promise<void>;
  readonly onSaveAll?: () => Promise<void>;
  readonly onDismissItem: (queryId: string) => void;
  readonly onSelectEvidence?: (segmentId: string) => void;
}

export const InnerOSUnsavedTray: React.FC<Props> = ({
  items,
  onSaveItem,
  onSaveAll,
  onDismissItem,
  onSelectEvidence,
}) => {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [savingIds, setSavingIds] = useState<Set<string>>(new Set());
  const [isSavingAll, setIsSavingAll] = useState(false);
  const [copiedAll, setCopiedAll] = useState(false);

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

  const handleSaveAllClick = async () => {
    if (!onSaveAll) return;
    setIsSavingAll(true);
    try {
      await onSaveAll();
    } finally {
      setIsSavingAll(false);
    }
  };

  const handleExportMarkdown = async () => {
    let md = `# 会中内心 OS 问答暂存记录\n\n`;
    items.forEach((item, idx) => {
      md += `### ${idx + 1}. Q: ${item.question}\n`;
      if (item.answer.facts.length > 0) {
        md += `**事实依据**:\n` + item.answer.facts.map((f) => `- ${f.text}`).join("\n") + "\n\n";
      }
      if (item.answer.judgements.length > 0) {
        md += `**局势研判**:\n` + item.answer.judgements.map((j) => `- ${j.text}`).join("\n") + "\n\n";
      }
      if (item.answer.draft?.text) {
        md += `**建议发言草稿**:\n> ${item.answer.draft.text}\n\n`;
      }
      md += `---\n\n`;
    });
    try {
      await navigator.clipboard.writeText(md.trim());
      setCopiedAll(true);
      setTimeout(() => setCopiedAll(false), 2000);
    } catch {
      // ignore
    }
  };

  return (
    <div className="inner-os-unsaved-tray" data-testid="inner-os-unsaved-tray">
      <div className="inner-os-tray-header">
        <div className="inner-os-tray-title">
          <span className="inner-os-tray-icon"><MaskIcon size={14} /></span>
          <span>会中内心 OS 问答暂存</span>
          {unsavedCount > 0 && (
            <span className="inner-os-tray-badge">{unsavedCount} 条待保存</span>
          )}
        </div>

        <div className="inner-os-tray-tools">
          <button
            type="button"
            className="inner-os-tray-tool-btn"
            onClick={handleExportMarkdown}
            title="复制暂存问答为 Markdown"
          >
            {copiedAll ? <><CheckIcon size={11} /> 已复制</> : <><DownloadIcon size={11} /> 导出暂存</>}
          </button>

          {unsavedCount > 0 && onSaveAll && (
            <button
              type="button"
              className="inner-os-tray-tool-btn is-primary"
              onClick={handleSaveAllClick}
              disabled={isSavingAll}
              title="一键将全部问答归档至会议记录"
            >
              {isSavingAll ? "保存中..." : `全部保存 (${unsavedCount})`}
            </button>
          )}

          <div className="inner-os-tray-ttl">
            <span title="会议结束后仍可在有效期内保存">30 分钟内可保存</span>
          </div>
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
                <button
                  type="button"
                  className="inner-os-tray-item-q"
                  onClick={() => setExpandedId(isExpanded ? null : item.queryId)}
                  aria-expanded={isExpanded}
                >
                  <span className="inner-os-tray-q-icon">Q:</span>
                  <span className="inner-os-tray-q-text">{item.question}</span>
                  <span className="inner-os-tray-expand-hint">
                    {isExpanded ? "收起" : "查看回答"}
                    <ChevronRightIcon className={isExpanded ? "is-expanded" : ""} size={12} />
                  </span>
                </button>
                <div className="inner-os-tray-item-actions">
                  <button
                    type="button"
                    className={`inner-os-tray-save-btn ${item.saved ? "is-saved" : ""}`}
                    onClick={() => handleSave(item.meetingId, item.queryId)}
                    disabled={item.saved || isSaving}
                  >
                    {item.saved ? "已保存" : isSaving ? "保存中..." : "保存"}
                  </button>
                  <button
                    type="button"
                    className="inner-os-tray-dismiss-btn"
                    onClick={() => onDismissItem(item.queryId)}
                    title="忽略并不保存"
                    aria-label="忽略并不保存"
                  >
                    <TrashIcon size={13} />
                  </button>
                </div>
              </div>

              {isExpanded && (
                <div className="inner-os-tray-item-detail">
                  <InnerOSAnswerContent
                    answer={item.answer}
                    onSelectEvidence={onSelectEvidence}
                    compact
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
