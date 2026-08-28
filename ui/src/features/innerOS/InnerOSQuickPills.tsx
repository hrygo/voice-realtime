import React, { useState } from "react";
import type { InnerOSIntent, QuickPromptCategory } from "./contracts";
import { useInnerOSStore } from "./innerOSStore";
import {
  EditIcon,
  PlusIcon,
  SparklesIcon,
  TrashIcon,
  WrenchIcon,
  XIcon,
  ZapIcon,
} from "../../components/Icons";

interface Props {
  readonly starredCount: number;
  readonly isFocusActive: boolean;
  readonly onToggleFocus: () => void;
  readonly onSelectQuickQuery: (question: string, intent: InnerOSIntent) => void;
  readonly activeCategory?: QuickPromptCategory;
  readonly onCategoryChange?: (category: QuickPromptCategory) => void;
  readonly disabled?: boolean;
}

const BUILTIN_PROMPTS: Record<
  "fact" | "analysis" | "draft",
  { readonly label: string; readonly intent: InnerOSIntent; readonly question: string }[]
> = {
  fact: [
    {
      label: "回顾刚才结论",
      intent: "fact",
      question: "刚才讨论的核心结论与明确共识是什么？",
    },
    {
      label: "排期与关键指标",
      intent: "fact",
      question: "刚才提到的核心性能指标、交付排期与验收时间节点是什么？",
    },
    {
      label: "未决分歧与争议",
      intent: "fact",
      question: "目前各方存在哪些尚未达成一致的争议或未决分歧点？",
    },
  ],
  analysis: [
    {
      label: "各方立场与潜在顾虑",
      intent: "analysis",
      question: "参会各方的核心诉求与立场是什么？是否有未明言的潜在顾虑与底线？",
    },
    {
      label: "方案漏洞与潜在风险",
      intent: "analysis",
      question: "针对刚才讨论的方案，有哪些潜在的技术盲区、排期延误或业务风险？",
    },
    {
      label: "承诺归属与权责分工",
      intent: "analysis",
      question: "刚才各方分别做出了哪些明确承诺？核心责任人与交付节点是谁？",
    },
  ],
  draft: [
    {
      label: "推动共识与下一步",
      intent: "draft",
      question: "帮我草拟一段得体、有力的发言，总结当前共识并推动下一步具体分工",
    },
    {
      label: "委婉提出异议/延期",
      intent: "draft",
      question: "帮我草拟一段委婉但逻辑坚挺的发言，得体地提出异议或合理争取排期缓冲",
    },
    {
      label: "切中要害的提问探底",
      intent: "draft",
      question: "帮我草拟一段礼貌但切中要害的质询发言，探寻对方方案的关键假设与顾虑",
    },
  ],
};

export const InnerOSQuickPills: React.FC<Props> = ({
  starredCount,
  isFocusActive,
  onToggleFocus,
  onSelectQuickQuery,
  activeCategory: propActiveCategory,
  onCategoryChange,
  disabled = false,
}) => {
  const [internalCategory, setInternalCategory] = useState<QuickPromptCategory>("fact");
  const activeCategory = propActiveCategory ?? internalCategory;

  const handleTabClick = (cat: QuickPromptCategory) => {
    setInternalCategory(cat);
    onCategoryChange?.(cat);
  };

  const [isAddingCustom, setIsAddingCustom] = useState(false);
  const [customLabel, setCustomLabel] = useState("");
  const [customQuestion, setCustomQuestion] = useState("");
  const [customIntent, setCustomIntent] = useState<InnerOSIntent>("mixed");

  const customPrompts = useInnerOSStore((s) => s.customPrompts);
  const addCustomPrompt = useInnerOSStore((s) => s.addCustomPrompt);
  const removeCustomPrompt = useInnerOSStore((s) => s.removeCustomPrompt);

  const handleSaveCustom = (e: React.FormEvent) => {
    e.preventDefault();
    if (!customLabel.trim() || !customQuestion.trim()) return;
    addCustomPrompt(customLabel, customQuestion, customIntent);
    setCustomLabel("");
    setCustomQuestion("");
    setIsAddingCustom(false);
  };

  const getCategoryIcon = (cat: QuickPromptCategory) => {
    switch (cat) {
      case "fact":
        return <ZapIcon size={12} />;
      case "analysis":
        return <WrenchIcon size={12} />;
      case "draft":
        return <EditIcon size={12} />;
      case "custom":
        return <SparklesIcon size={12} />;
    }
  };

  return (
    <div className="inner-os-quick-section">
      {starredCount > 0 && (
        <div className="inner-os-focus-indicator">
          <div
            className="inner-os-focus-badge"
            aria-label={`重点片段 ${starredCount} 段，${isFocusActive ? "已启用优先检索" : "未启用优先检索"}`}
          >
            <SparklesIcon className="inner-os-star-icon" size={14} />
            <span className="inner-os-focus-label">重点 {starredCount} 段</span>
            <span className="inner-os-focus-separator" aria-hidden="true">·</span>
            <span className={`inner-os-focus-status ${isFocusActive ? "is-active" : ""}`}>
              {isFocusActive ? "优先检索" : "未加权"}
            </span>
          </div>
          <button
            type="button"
            className="inner-os-focus-btn"
            onClick={onToggleFocus}
            aria-pressed={isFocusActive}
            title={isFocusActive ? "点击暂时解除对重点发言的优先检索" : "点击优先检索重点发言"}
          >
            {isFocusActive ? <><XIcon size={12} /> 解除加权</> : "启用加权"}
          </button>
        </div>
      )}

      {/* Category Tabs */}
      <div className="inner-os-quick-tabs">
        {(["fact", "analysis", "draft", "custom"] as QuickPromptCategory[]).map((cat) => {
          const tabMeta: Record<QuickPromptCategory, { icon: React.ReactNode; label: string }> = {
            fact: { icon: <ZapIcon size={12} />, label: "事实核查" },
            analysis: { icon: <WrenchIcon size={12} />, label: "局势研判" },
            draft: { icon: <EditIcon size={12} />, label: "回应草稿" },
            custom: { icon: <SparklesIcon size={12} />, label: "常用指令" },
          };
          const item = tabMeta[cat];
          return (
            <button
              key={cat}
              type="button"
              className={`inner-os-tab-btn ${activeCategory === cat ? "is-active" : ""}`}
              onClick={() => handleTabClick(cat)}
            >
              <span className="inner-os-tab-icon">{item.icon}</span>
              <span>{item.label}</span>
            </button>
          );
        })}
      </div>

      {/* Prompts list for active category */}
      <div className="inner-os-quick-pills">
        {activeCategory !== "custom" &&
          BUILTIN_PROMPTS[activeCategory].map((prompt) => (
            <button
              key={prompt.label}
              type="button"
              className="inner-os-quick-pill-btn"
              onClick={() => onSelectQuickQuery(prompt.question, prompt.intent)}
              disabled={disabled}
              title={prompt.question}
            >
              <span className="inner-os-pill-icon">{getCategoryIcon(activeCategory)}</span>
              <span className="inner-os-pill-label">{prompt.label}</span>
            </button>
          ))}

        {activeCategory === "custom" && (
          <>
            {customPrompts.length === 0 && !isAddingCustom && (
              <div className="inner-os-custom-empty">
                <span>暂无自定义快捷 Prompt</span>
              </div>
            )}

            {customPrompts.map((cp) => (
              <div key={cp.id} className="inner-os-custom-pill-wrap">
                <button
                  type="button"
                  className="inner-os-quick-pill-btn is-custom"
                  onClick={() => onSelectQuickQuery(cp.question, cp.intent)}
                  disabled={disabled}
                  title={cp.question}
                >
                  <span className="inner-os-pill-icon"><SparklesIcon size={12} /></span>
                  <span className="inner-os-pill-label">{cp.label}</span>
                </button>
                <button
                  type="button"
                  className="inner-os-custom-del-btn"
                  onClick={() => removeCustomPrompt(cp.id)}
                  title="删除此常用 Prompt"
                >
                  <TrashIcon size={11} />
                </button>
              </div>
            ))}

            {!isAddingCustom && (
              <button
                type="button"
                className="inner-os-add-prompt-btn"
                onClick={() => setIsAddingCustom(true)}
              >
                <PlusIcon size={12} /> 添加常用 Prompt
              </button>
            )}
          </>
        )}
      </div>

      {/* Add Custom Prompt Form */}
      {activeCategory === "custom" && isAddingCustom && (
        <form className="inner-os-add-prompt-form" onSubmit={handleSaveCustom}>
          <div className="inner-os-form-row">
            <input
              type="text"
              placeholder="指令简称 (如: 洞察潜台词)"
              value={customLabel}
              onChange={(e) => setCustomLabel(e.target.value)}
              maxLength={20}
              required
            />
            <select
              value={customIntent}
              onChange={(e) => setCustomIntent(e.target.value as InnerOSIntent)}
            >
              <option value="mixed">综合研判</option>
              <option value="fact">事实核查</option>
              <option value="analysis">局势研判</option>
              <option value="draft">回应草稿</option>
            </select>
          </div>
          <textarea
            rows={2}
            placeholder="输入定制的提问指令 (如: 分析对方发言中未明言的商业诉求与让步空间)..."
            value={customQuestion}
            onChange={(e) => setCustomQuestion(e.target.value)}
            required
          />
          <div className="inner-os-form-actions">
            <button
              type="button"
              className="inner-os-form-cancel"
              onClick={() => setIsAddingCustom(false)}
            >
              取消
            </button>
            <button type="submit" className="inner-os-form-submit">
              保存常用
            </button>
          </div>
        </form>
      )}
    </div>
  );
};
