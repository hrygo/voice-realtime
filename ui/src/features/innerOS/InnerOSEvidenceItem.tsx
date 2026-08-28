import React, { useState } from "react";
import type { InnerOSEvidenceItem as EvidenceType } from "./contracts";
import { ChevronRightIcon, FileTextIcon, UserIcon } from "../../components/Icons";

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
  const [showTooltip, setShowTooltip] = useState(false);
  const label = `S${String(index + 1).padStart(4, "0")}`;
  const timeStr = formatTime(evidence.start_ms);

  const handleClick = (e: React.MouseEvent) => {
    e.preventDefault();
    if (onSelectEvidence) {
      onSelectEvidence(evidence.segment_id);
    }
  };

  return (
    <div
      className="inner-os-evidence-wrapper"
      onMouseEnter={() => setShowTooltip(true)}
      onMouseLeave={() => setShowTooltip(false)}
      onFocus={() => setShowTooltip(true)}
      onBlur={() => setShowTooltip(false)}
    >
      <button
        type="button"
        className="inner-os-evidence-pill"
        onClick={handleClick}
        aria-label={`证据 ${label}，${evidence.speaker_name} 发言于 ${timeStr}，点击定位至转录`}
        data-testid={`evidence-pill-${evidence.segment_id}`}
      >
        <FileTextIcon className="inner-os-evidence-icon" size={12} />
        <span className="inner-os-evidence-tag">{label}</span>
        <span className="inner-os-evidence-speaker">{evidence.speaker_name}</span>
        <span className="inner-os-evidence-time">{timeStr}</span>
        <ChevronRightIcon className="inner-os-evidence-arrow" size={12} />
      </button>

      {showTooltip && (
        <div className="inner-os-evidence-tooltip" role="tooltip">
          <div className="inner-os-tooltip-header">
            <UserIcon size={12} />
            <strong>{evidence.speaker_name}</strong>
            <span className="inner-os-tooltip-time">{timeStr}</span>
          </div>
          <p className="inner-os-tooltip-text">{evidence.text}</p>
          <div className="inner-os-tooltip-hint">点击可平滑滚动定位至转录流</div>
        </div>
      )}
    </div>
  );
};
