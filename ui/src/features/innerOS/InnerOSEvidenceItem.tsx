import React from "react";
import type { InnerOSEvidenceItem as EvidenceType } from "./contracts";

interface Props {
  readonly evidence: EvidenceType;
  readonly index: number;
  readonly onSelectEvidence?: (segmentId: string) => void;
}

function formatTime(ms: number): string {
  const totalSecs = Math.floor(ms / 1000);
  const mins = Math.floor(totalSecs / 60);
  const secs = totalSecs % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

export const InnerOSEvidenceItem: React.FC<Props> = ({
  evidence,
  index,
  onSelectEvidence,
}) => {
  const label = `S${String(index + 1).padStart(4, "0")}`;

  const handleClick = (e: React.MouseEvent) => {
    e.preventDefault();
    if (onSelectEvidence) {
      onSelectEvidence(evidence.segment_id);
    }
  };

  return (
    <button
      type="button"
      className="inner-os-evidence-pill"
      onClick={handleClick}
      title={`${evidence.speaker_name} (${formatTime(evidence.start_ms)}): ${evidence.text}`}
      data-testid={`evidence-pill-${evidence.segment_id}`}
    >
      <span className="inner-os-evidence-icon">📎</span>
      <span className="inner-os-evidence-tag">{label}</span>
      <span className="inner-os-evidence-speaker">{evidence.speaker_name}</span>
      <span className="inner-os-evidence-time">{formatTime(evidence.start_ms)}</span>
      <span className="inner-os-evidence-arrow">↗</span>
    </button>
  );
};
