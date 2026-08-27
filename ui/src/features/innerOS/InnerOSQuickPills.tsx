import React from "react";
import type { InnerOSIntent } from "./contracts";

interface Props {
  readonly starredCount: number;
  readonly isFocusActive: boolean;
  readonly onToggleFocus: () => void;
  readonly onSelectQuickQuery: (question: string, intent: InnerOSIntent) => void;
  readonly disabled?: boolean;
}

const QUICK_PROMPTS: {
  readonly label: string;
  readonly icon: string;
  readonly intent: InnerOSIntent;
  readonly question: string;
}[] = [
  {
    icon: "⚡",
    label: "回顾刚才结论",
    intent: "fact",
    question: "刚才讨论的核心结论与明确共识是什么？",
  },
  {
    icon: "🤝",
    label: "明确承诺分工",
    intent: "analysis",
    question: "刚才各位参会者分别做出了哪些承诺或具体待办分工？",
  },
  {
    icon: "📝",
    label: "草拟回应建议",
    intent: "draft",
    question: "结合当前上下文与临时目标，帮我草拟一段得体、专业的回应发言草稿",
  },
];

export const InnerOSQuickPills: React.FC<Props> = ({
  starredCount,
  isFocusActive,
  onToggleFocus,
  onSelectQuickQuery,
  disabled = false,
}) => {
  return (
    <div className="inner-os-quick-section">
      {starredCount > 0 && (
        <div className="inner-os-focus-indicator">
          <div className="inner-os-focus-badge">
            <span className="inner-os-star-icon">⭐</span>
            <span>已标记 {starredCount} 段重点发言</span>
            {isFocusActive ? (
              <span className="inner-os-focus-status is-active">(优先检索中)</span>
            ) : (
              <span className="inner-os-focus-status">(未启用加权)</span>
            )}
          </div>
          <button
            type="button"
            className="inner-os-focus-btn"
            onClick={onToggleFocus}
            title={isFocusActive ? "点击暂时解除对重点发言的优先检索" : "点击优先检索重点发言"}
          >
            {isFocusActive ? "✕ 解除加权" : "✓ 启用加权"}
          </button>
        </div>
      )}

      <div className="inner-os-quick-pills">
        {QUICK_PROMPTS.map((prompt) => (
          <button
            key={prompt.label}
            type="button"
            className="inner-os-quick-pill-btn"
            onClick={() => onSelectQuickQuery(prompt.question, prompt.intent)}
            disabled={disabled}
          >
            <span className="inner-os-pill-icon">{prompt.icon}</span>
            <span className="inner-os-pill-label">{prompt.label}</span>
          </button>
        ))}
      </div>
    </div>
  );
};
