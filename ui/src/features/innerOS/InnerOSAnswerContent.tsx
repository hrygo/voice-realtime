import React, { useState } from "react";
import "./InnerOSAnswerCard.css";
import type { DraftTone, InnerOSAnswer, InnerOSUncertainty } from "./contracts";
import { InnerOSEvidenceItem } from "./InnerOSEvidenceItem";
import { innerOSMetrics } from "./metrics";
import {
  CheckIcon,
  CopyIcon,
  EditIcon,
  FileTextIcon,
  SparklesIcon,
  WrenchIcon,
} from "../../components/Icons";

interface Props {
  readonly answer: InnerOSAnswer;
  readonly onSelectEvidence?: (segmentId: string) => void;
  readonly compact?: boolean;
}

const UNCERTAINTY_MAP: Record<
  InnerOSUncertainty,
  { label: string; className: string }
> = {
  low: { label: "低不确定性", className: "badge-low" },
  medium: { label: "中不确定性", className: "badge-medium" },
  high: { label: "高不确定性", className: "badge-high" },
};

function formatDraftTone(original: string, tone: DraftTone): string {
  if (!original) return "";
  switch (tone) {
    case "concise": {
      // Condense to first or key sentence
      const sentences = original.split(/[。！？\n]/).filter((s) => s.trim().length > 0);
      return sentences.length > 0 ? `${sentences[0]}。` : original;
    }
    case "constructive": {
      // Prefix with constructive agreement
      if (original.startsWith("建议") || original.startsWith("关于")) {
        return `赞同当前方向。${original}`;
      }
      return `关于这一点，${original}`;
    }
    case "inquisitive": {
      // Suffix with clarification question
      return `${original} 各位怎么看？是否还有其他考量？`;
    }
    case "professional":
    default:
      return original;
  }
}

export const InnerOSAnswerContent: React.FC<Props> = ({
  answer,
  onSelectEvidence,
  compact = false,
}) => {
  const [copiedDraft, setCopiedDraft] = useState(false);
  const [copiedFacts, setCopiedFacts] = useState(false);
  const [activeTone, setActiveTone] = useState<DraftTone>("professional");
  const evidenceMap = new Map(answer.evidence.map((ev, idx) => [ev.segment_id, { ev, idx }]));

  const currentDraftText = answer.draft?.text
    ? formatDraftTone(answer.draft.text, activeTone)
    : "";

  const handleCopyDraft = async () => {
    if (!currentDraftText) return;
    try {
      await navigator.clipboard.writeText(currentDraftText);
      setCopiedDraft(true);
      innerOSMetrics.recordDraftCopied();
      setTimeout(() => setCopiedDraft(false), 2000);
    } catch {
      // Clipboard fallback
    }
  };

  const handleCopyFacts = async () => {
    if (answer.facts.length === 0) return;
    const text = answer.facts.map((f, i) => `${i + 1}. ${f.text}`).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopiedFacts(true);
      setTimeout(() => setCopiedFacts(false), 2000);
    } catch {
      // Clipboard fallback
    }
  };

  return (
    <div className={`inner-os-card-body inner-os-answer-content ${compact ? "is-compact" : ""}`}>
      {/* 1. 事实依据层 (Facts) */}
      {answer.facts.length > 0 && (
        <section className="inner-os-tier-section tier-facts">
          <div className="inner-os-tier-header">
            <div className="inner-os-tier-title">
              <FileTextIcon className="inner-os-tier-icon" size={14} />
              <span>事实依据</span>
              <span className="inner-os-count-badge">{answer.facts.length} 条</span>
            </div>
            {!compact && (
              <button
                type="button"
                className="inner-os-mini-action-btn"
                onClick={handleCopyFacts}
                title="复制全部事实依据"
              >
                {copiedFacts ? <><CheckIcon size={11} /> 已复制</> : <><CopyIcon size={11} /> 复制要点</>}
              </button>
            )}
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
        </section>
      )}

      {/* 2. 局势判断层 (Judgements) */}
      {answer.judgements.length > 0 && (
        <section className="inner-os-tier-section tier-judgements">
          <div className="inner-os-tier-header">
            <div className="inner-os-tier-title">
              <WrenchIcon className="inner-os-tier-icon" size={14} />
              <span>局势判断与洞察</span>
              <span className="inner-os-count-badge">{answer.judgements.length} 项</span>
            </div>
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
        </section>
      )}

      {/* 3. 建议发言草稿层 (Draft) */}
      {answer.draft?.text && (
        <section className="inner-os-tier-section tier-draft">
          <div className="inner-os-tier-header">
            <div className="inner-os-tier-title">
              <EditIcon className="inner-os-tier-icon" size={14} />
              <span>建议发言草稿</span>
            </div>
            {!compact && (
              <div className="inner-os-tone-selector">
                <span className="inner-os-tone-label"><SparklesIcon size={11} /> 语气:</span>
                {(["professional", "concise", "constructive", "inquisitive"] as DraftTone[]).map((tone) => {
                  const labels: Record<DraftTone, string> = {
                    professional: "标准",
                    concise: "简短",
                    constructive: "推进",
                    inquisitive: "探寻",
                  };
                  return (
                    <button
                      key={tone}
                      type="button"
                      className={`inner-os-tone-chip ${activeTone === tone ? "is-active" : ""}`}
                      onClick={() => setActiveTone(tone)}
                      title={`切换为${labels[tone]}语气`}
                    >
                      {labels[tone]}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <div className="inner-os-draft-box">
            <div className="inner-os-draft-quote-glyph">“</div>
            <div className="inner-os-draft-text">{currentDraftText}</div>
            <div className="inner-os-draft-actions">
              <button
                type="button"
                className={`inner-os-copy-btn ${copiedDraft ? "is-copied" : ""}`}
                onClick={handleCopyDraft}
                title="复制发言草稿到剪贴板 (⌘+Shift+C)"
              >
                {copiedDraft ? (
                  <><CheckIcon size={13} /> 已复制到剪贴板</>
                ) : (
                  <><CopyIcon size={13} /> 复制发言草稿</>
                )}
              </button>
            </div>
          </div>
        </section>
      )}

      {/* 4. 局限与说明层 (Limitations) */}
      {answer.limitations.length > 0 && (
        <section className="inner-os-tier-section tier-limitations">
          {answer.limitations.map((lim, lIdx) => (
            <div key={`lim-${lIdx}`} className="inner-os-limitation-item">
              <span className="inner-os-limitation-icon" aria-hidden="true">!</span>
              <span className="inner-os-limitation-msg">{lim.message}</span>
            </div>
          ))}
        </section>
      )}
    </div>
  );
};
