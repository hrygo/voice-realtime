import React, { useState } from "react";
import type { InnerOSEphemeralContext as EphemeralContextType } from "./contracts";

interface Props {
  readonly context: EphemeralContextType;
  readonly version: number;
  readonly onChange: (nextContext: EphemeralContextType) => void;
  readonly onClear: () => void;
}

export const InnerOSEphemeralContextDrawer: React.FC<Props> = ({
  context,
  version,
  onChange,
  onClear,
}) => {
  const [isOpen, setIsOpen] = useState(false);

  const hasContent = Boolean(context.goal || context.agenda || context.background);

  return (
    <div className={`inner-os-ephemeral-drawer ${isOpen ? "is-open" : ""}`}>
      <button
        type="button"
        className="inner-os-ephemeral-toggle"
        onClick={() => setIsOpen(!isOpen)}
        aria-expanded={isOpen}
      >
        <div className="inner-os-ephemeral-title">
          <span className="inner-os-ephemeral-icon">📝</span>
          <span className="inner-os-ephemeral-label">本次临时目标/背景</span>
          <span className="inner-os-ephemeral-badge">仅内存·即焚</span>
          {hasContent && <span className="inner-os-ephemeral-dot" title="已有设定内容" />}
        </div>
        <div className="inner-os-ephemeral-meta">
          <span className="inner-os-ephemeral-version">v{version}</span>
          <span className="inner-os-chevron">{isOpen ? "▲" : "▼"}</span>
        </div>
      </button>

      {isOpen && (
        <div className="inner-os-ephemeral-body">
          <div className="inner-os-ephemeral-field">
            <label htmlFor="inner-os-goal">🎯 核心目标 (Goal)</label>
            <input
              id="inner-os-goal"
              type="text"
              placeholder="例如：确保周五前完成网关验收，且延迟 < 15ms"
              value={context.goal || ""}
              onChange={(e) => onChange({ ...context, goal: e.target.value })}
              maxLength={1000}
            />
          </div>

          <div className="inner-os-ephemeral-field">
            <label htmlFor="inner-os-agenda">📋 关键议题 (Agenda)</label>
            <input
              id="inner-os-agenda"
              type="text"
              placeholder="例如：1. 性能指标；2. 灰度策略；3. 回滚预案"
              value={context.agenda || ""}
              onChange={(e) => onChange({ ...context, agenda: e.target.value })}
              maxLength={1000}
            />
          </div>

          <div className="inner-os-ephemeral-field">
            <label htmlFor="inner-os-background">🔒 私密背景/底线 (Background)</label>
            <textarea
              id="inner-os-background"
              rows={2}
              placeholder="例如：团队人力不足，若要求周三交付需明确拒绝"
              value={context.background || ""}
              onChange={(e) => onChange({ ...context, background: e.target.value })}
              maxLength={2000}
            />
          </div>

          <div className="inner-os-ephemeral-footer">
            <span className="inner-os-ephemeral-tip">💡 提示：本区域内容在刷新页面或结束会议后将立即清空，绝不落库。</span>
            {hasContent && (
              <button
                type="button"
                className="inner-os-ephemeral-clear-btn"
                onClick={onClear}
              >
                清空重置
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
};
