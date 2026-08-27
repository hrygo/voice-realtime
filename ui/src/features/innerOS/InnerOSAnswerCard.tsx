import React, { useState } from "react";
import type { InnerOSAnswer, InnerOSIntent, InnerOSUncertainty } from "./contracts";
import { InnerOSEvidenceItem } from "./InnerOSEvidenceItem";
import { innerOSMetrics } from "./metrics";

interface Props {
  readonly queryId: string;
  readonly question: string;
  readonly intent: InnerOSIntent;
  readonly answer: InnerOSAnswer;
  readonly saved: boolean;
  readonly onSave: () => void;
  readonly onFollowUp?: (question: string) => void;
  readonly onSelectEvidence?: (segmentId: string) => void;
  readonly isSaving?: boolean;
}

const INTENT_LABELS: Record<InnerOSIntent, string> = {
  fact: "📋 事实核查",
  analysis: "🧠 局势判断",
  draft: "✍️ 回应草稿",
  mixed: "⚡ 综合研判",
};

const UNCERTAINTY_MAP: Record<
  InnerOSUncertainty,
  { label: string; className: string }
> = {
  low: { label: "低不确定性", className: "badge-low" },
  medium: { label: "中不确定性", className: "badge-medium" },
  high: { label: "高不确定性", className: "badge-high" },
};

export const InnerOSAnswerCard: React.FC<Props> = ({
  queryId,
  question,
  intent,
  answer,
  saved,
  onSave,
  onFollowUp,
  onSelectEvidence,
  isSaving = false,
}) => {
  const [copiedDraft, setCopiedDraft] = useState(false);

  const handleCopyDraft = async () => {
    if (!answer.draft?.text) return;
    try {
      await navigator.clipboard.writeText(answer.draft.text);
      setCopiedDraft(true);
      innerOSMetrics.recordDraftCopied();
      setTimeout(() => setCopiedDraft(false), 2000);
    } catch {
      // clipboard fallback
    }
  };

  const evidenceMap = new Map(answer.evidence.map((ev, idx) => [ev.segment_id, { ev, idx }]));

  return (
    <div className="inner-os-answer-card" data-testid={`answer-card-${queryId}`}>
      <div className="inner-os-card-header">
        <div className="inner-os-question-wrap">
          <span className="inner-os-q-tag">Q</span>
          <span className="inner-os-question-text">{question}</span>
        </div>
        <span className="inner-os-intent-badge">{INTENT_LABELS[intent] || intent}</span>
      </div>

      <div className="inner-os-card-body">
        {/* Tier 1: Facts */}
        {answer.facts.length > 0 && (
          <div className="inner-os-tier-section tier-facts">
            <div className="inner-os-tier-title">
              <span className="inner-os-tier-icon">📋</span>
              <span>事实依据 (Facts)</span>
            </div>
            <ul className="inner-os-tier-list">
              {answer.facts.map((fact, fIdx) => (
                <li key={`fact-${fIdx}`} className="inner-os-fact-item">
                  <div className="inner-os-fact-text">{fact.text}</div>
                  {fact.evidence_segment_ids.length > 0 && (
                    <div className="inner-os-evidence-group">
                      {fact.evidence_segment_ids.map((segId) => {
                        const target = evidenceMap.get(segId);
                        if (!target) return null;
                        return (
                          <InnerOSEvidenceItem
                            key={segId}
                            evidence={target.ev}
                            index={target.idx}
                            onSelectEvidence={onSelectEvidence}
                          />
                        );
                      })}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Tier 2: Judgements */}
        {answer.judgements.length > 0 && (
          <div className="inner-os-tier-section tier-judgements">
            <div className="inner-os-tier-title">
              <span className="inner-os-tier-icon">🧠</span>
              <span>模型判断与局势评估 (Judgements)</span>
            </div>
            <ul className="inner-os-tier-list">
              {answer.judgements.map((judgement, jIdx) => {
                const badge = UNCERTAINTY_MAP[judgement.uncertainty] || UNCERTAINTY_MAP.low;
                return (
                  <li key={`judgement-${jIdx}`} className="inner-os-judgement-item">
                    <div className="inner-os-judgement-text">{judgement.text}</div>
                    <div className="inner-os-judgement-meta">
                      <span className={`inner-os-uncertainty-badge ${badge.className}`}>
                        {badge.label}
                      </span>
                      <span className="inner-os-uncertainty-reason">
                        依据: {judgement.uncertainty_reason}
                      </span>
                    </div>
                    {judgement.basis_segment_ids.length > 0 && (
                      <div className="inner-os-evidence-group">
                        {judgement.basis_segment_ids.map((segId) => {
                          const target = evidenceMap.get(segId);
                          if (!target) return null;
                          return (
                            <InnerOSEvidenceItem
                              key={segId}
                              evidence={target.ev}
                              index={target.idx}
                              onSelectEvidence={onSelectEvidence}
                            />
                          );
                        })}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {/* Tier 3: Suggested Draft */}
        {answer.draft?.text && (
          <div className="inner-os-tier-section tier-draft">
            <div className="inner-os-tier-title">
              <span className="inner-os-tier-icon">✍️</span>
              <span>建议回应草稿 (Draft · 仅供参考)</span>
            </div>
            <div className="inner-os-draft-box">
              <div className="inner-os-draft-text">{answer.draft.text}</div>
              <div className="inner-os-draft-actions">
                <button
                  type="button"
                  className={`inner-os-copy-btn ${copiedDraft ? "is-copied" : ""}`}
                  onClick={handleCopyDraft}
                >
                  {copiedDraft ? "✓ 已复制" : "📋 复制发言草稿"}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Tier 4: Limitations */}
        {answer.limitations.length > 0 && (
          <div className="inner-os-tier-section tier-limitations">
            {answer.limitations.map((lim, lIdx) => (
              <div key={`lim-${lIdx}`} className="inner-os-limitation-item">
                <span className="inner-os-limitation-icon">⚠️</span>
                <span className="inner-os-limitation-msg">{lim.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="inner-os-card-footer">
        <div className="inner-os-footer-left">
          <button
            type="button"
            className={`inner-os-save-btn ${saved ? "is-saved" : ""}`}
            onClick={onSave}
            disabled={saved || isSaving}
          >
            {saved ? "✓ 已保存至会议档案" : isSaving ? "⏳ 保存中..." : "💾 保存此条问答"}
          </button>
        </div>
        <div className="inner-os-footer-right">
          {onFollowUp && (
            <button
              type="button"
              className="inner-os-followup-btn"
              onClick={() => onFollowUp(`关于刚才提到的：${question}`)}
            >
              🔄 追问
            </button>
          )}
        </div>
      </div>
    </div>
  );
};
