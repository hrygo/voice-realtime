import React, { useState } from "react";
import type { InnerOSAnswer, InnerOSIntent } from "./contracts";
import { InnerOSAnswerContent } from "./InnerOSAnswerContent";
import {
  CheckIcon,
  ChevronRightIcon,
  CopyIcon,
  FileTextIcon,
  SparklesIcon,
  TrashIcon,
} from "../../components/Icons";

interface Props {
  readonly queryId: string;
  readonly question: string;
  readonly intent: InnerOSIntent;
  readonly answer: InnerOSAnswer;
  readonly saved: boolean;
  readonly onSave: () => void;
  readonly onDelete?: () => void;
  readonly onFollowUp?: (question: string) => void;
  readonly onSelectEvidence?: (segmentId: string) => void;
  readonly isSaving?: boolean;
  readonly createdAt?: string;
  readonly isCollapsible?: boolean;
  readonly defaultExpanded?: boolean;
}

const INTENT_LABELS: Record<InnerOSIntent, string> = {
  fact: "事实核查",
  analysis: "局势研判",
  draft: "回应草稿",
  mixed: "综合研判",
};

export const InnerOSAnswerCard: React.FC<Props> = ({
  queryId,
  question,
  intent,
  answer,
  saved,
  onSave,
  onDelete,
  onFollowUp,
  onSelectEvidence,
  isSaving = false,
  createdAt,
  isCollapsible = false,
  defaultExpanded = true,
}) => {
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);
  const [copiedAll, setCopiedAll] = useState(false);

  const timeStr = createdAt
    ? new Date(createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : undefined;

  const handleCopyFullMarkdown = async () => {
    let md = `### Q: ${question}\n\n`;
    if (answer.facts.length > 0) {
      md += `**事实依据**:\n` + answer.facts.map((f) => `- ${f.text}`).join("\n") + "\n\n";
    }
    if (answer.judgements.length > 0) {
      md += `**局势研判**:\n` + answer.judgements.map((j) => `- ${j.text} (${j.uncertainty_reason})`).join("\n") + "\n\n";
    }
    if (answer.draft?.text) {
      md += `**建议发言草稿**:\n> ${answer.draft.text}\n\n`;
    }
    try {
      await navigator.clipboard.writeText(md.trim());
      setCopiedAll(true);
      setTimeout(() => setCopiedAll(false), 2000);
    } catch {
      // ignore
    }
  };

  return (
    <div
      className={`inner-os-answer-card ${isExpanded ? "is-expanded" : "is-collapsed"}`}
      data-testid={`answer-card-${queryId}`}
    >
      <div className="inner-os-card-header">
        <div
          className="inner-os-question-wrap"
          onClick={isCollapsible ? () => setIsExpanded(!isExpanded) : undefined}
          style={{ cursor: isCollapsible ? "pointer" : "default" }}
        >
          {isCollapsible && (
            <ChevronRightIcon
              className={`inner-os-card-chevron ${isExpanded ? "is-expanded" : ""}`}
              size={13}
            />
          )}
          <span className="inner-os-q-tag">Q</span>
          <span className="inner-os-question-text">{question}</span>
        </div>

        <div className="inner-os-header-meta">
          {timeStr && <span className="inner-os-time-tag">{timeStr}</span>}
          <span className="inner-os-intent-badge">{INTENT_LABELS[intent] || intent}</span>
        </div>
      </div>

      {isExpanded && (
        <>
          <InnerOSAnswerContent answer={answer} onSelectEvidence={onSelectEvidence} />

          <div className="inner-os-card-footer">
            <div className="inner-os-footer-left">
              <button
                type="button"
                className={`inner-os-save-btn ${saved ? "is-saved" : ""}`}
                onClick={onSave}
                disabled={saved || isSaving}
                title={saved ? "已保存至本场会议记录" : "点击保存此条问答"}
              >
                {saved ? (
                  <><CheckIcon size={12} /> 已保存</>
                ) : isSaving ? (
                  "保存中..."
                ) : (
                  <><FileTextIcon size={12} /> 保存</>
                )}
              </button>

              {onDelete && (
                <button
                  type="button"
                  className="inner-os-delete-btn"
                  onClick={onDelete}
                  title="删除此条问答"
                >
                  <TrashIcon size={12} /> 删除
                </button>
              )}

              <button
                type="button"
                className="inner-os-copy-full-btn"
                onClick={handleCopyFullMarkdown}
                title="复制整条问答 (Markdown)"
              >
                {copiedAll ? <><CheckIcon size={12} /> 已复制</> : <><CopyIcon size={12} /> 复制</>}
              </button>
            </div>

            <div className="inner-os-footer-right">
              {onFollowUp && (
                <button
                  type="button"
                  className="inner-os-followup-btn"
                  onClick={() => onFollowUp(`关于刚才提到的：${question}`)}
                  title="基于此条问答继续追问"
                >
                  <SparklesIcon size={13} /> 追问
                </button>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
};
