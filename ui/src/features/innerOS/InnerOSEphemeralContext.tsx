import React, { useState } from "react";
import type { InnerOSEphemeralContext as EphemeralContextType } from "./contracts";
import {
  BroomIcon,
  CheckIcon,
  ChevronRightIcon,
  FileTextIcon,
  MaskIcon,
  SparklesIcon,
} from "../../components/Icons";

interface Props {
  readonly context: EphemeralContextType;
  readonly onChange: (nextContext: EphemeralContextType) => void;
  readonly onClear: () => void;
}

const PRESET_TEMPLATES = [
  {
    name: "方案评审",
    goal: "达成架构与选型共识，明确交付里程碑与兜底防线",
    agenda: "1. 架构方案与技术债务；2. 压测与 SLA 指标；3. 灰度上线与回滚预案",
    background: "重点核实接口变更与容量预估，对未评估风险的工期压缩保持审慎",
  },
  {
    name: "商务谈判",
    goal: "锁定关键商务条款与服务 SLA，争取最优合作权益",
    agenda: "1. 报价区间与商务条款；2. 交付周期；3. 售后与惩罚机制",
    background: "底线是总预算不上浮超过 10%，排期至少保留 2 周安全缓冲期",
  },
  {
    name: "述职答辩",
    goal: "突出业务核心产出与技术深度，从容应对评委质询",
    agenda: "1. 关键业绩与业务突破；2. 难点攻坚；3. 未来演进规划",
    background: "重点突出自主架构设计与量化降本提效数据，从容化解质疑",
  },
  {
    name: "跨组协同",
    goal: "拉齐各方排期依赖，明确接口交付人与责任界限",
    agenda: "1. 依赖接口定义；2. 联调节点；3. 阻塞项清除与权责分工",
    background: "确保对方明确承诺测试环境就绪时间，避免模糊口头承诺",
  },
];

export const InnerOSEphemeralContextDrawer: React.FC<Props> = ({
  context,
  onChange,
  onClear,
}) => {
  const [isOpen, setIsOpen] = useState(false);

  const hasContent = Boolean(context.goal || context.agenda || context.background);

  const activePreset = PRESET_TEMPLATES.find(
    (p) =>
      p.goal === context.goal &&
      p.agenda === context.agenda &&
      p.background === context.background,
  );

  const handleApplyPreset = (preset: typeof PRESET_TEMPLATES[0]) => {
    onChange({
      goal: preset.goal,
      agenda: preset.agenda,
      background: preset.background,
    });
  };

  return (
    <div className={`inner-os-ephemeral-drawer ${isOpen ? "is-open" : ""} ${hasContent ? "has-content" : ""}`}>
      <button
        type="button"
        className="inner-os-ephemeral-toggle"
        onClick={() => setIsOpen(!isOpen)}
        aria-expanded={isOpen}
      >
        <div className="inner-os-ephemeral-title">
          <span className="inner-os-ephemeral-icon"><MaskIcon size={14} /></span>
          <span className="inner-os-ephemeral-label">会前底牌与目标</span>
          <span className="inner-os-ephemeral-badge">{hasContent ? "已生效" : "会后即焚"}</span>
          {hasContent && (
            <span className="inner-os-ephemeral-summary" title={context.goal || context.background || "已设定私密立场"}>
              {context.goal || context.background}
            </span>
          )}
        </div>
        <div className="inner-os-ephemeral-meta">
          <span className="inner-os-ephemeral-hint">{isOpen ? "收起" : hasContent ? "修改" : "设定"}</span>
          <ChevronRightIcon
            className={`inner-os-chevron ${isOpen ? "is-expanded" : ""}`}
            size={13}
            aria-hidden="true"
          />
        </div>
      </button>

      {isOpen && (
        <div className="inner-os-ephemeral-body">
          {/* Quick preset templates */}
          <div className="inner-os-preset-bar">
            <span className="inner-os-preset-label"><SparklesIcon size={12} /> 场景预设:</span>
            <div className="inner-os-preset-chips">
              {PRESET_TEMPLATES.map((p) => {
                const isSelected = activePreset?.name === p.name;
                return (
                  <button
                    key={p.name}
                    type="button"
                    className={`inner-os-preset-chip ${isSelected ? "is-selected" : ""}`}
                    onClick={() => handleApplyPreset(p)}
                    title={`一键套用「${p.name}」场景设定`}
                  >
                    {isSelected && <CheckIcon size={10} />}
                    <span>{p.name}</span>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="inner-os-ephemeral-row">
            <label htmlFor="inner-os-goal" className="inner-os-ephemeral-label-inline">
              <FileTextIcon size={12} />
              <span>核心目标</span>
            </label>
            <input
              id="inner-os-goal"
              type="text"
              className="inner-os-ephemeral-input-inline"
              placeholder="期望达成的理想结果 (如: 锁定核心责任人与联合排期)"
              value={context.goal || ""}
              onChange={(e) => onChange({ ...context, goal: e.target.value })}
              maxLength={1000}
            />
          </div>

          <div className="inner-os-ephemeral-row">
            <label htmlFor="inner-os-agenda" className="inner-os-ephemeral-label-inline">
              <FileTextIcon size={12} />
              <span>关键议题</span>
            </label>
            <input
              id="inner-os-agenda"
              type="text"
              className="inner-os-ephemeral-input-inline"
              placeholder="重点聚焦的争鸣议题 (如: 1. 架构选型; 2. SLA 指标)"
              value={context.agenda || ""}
              onChange={(e) => onChange({ ...context, agenda: e.target.value })}
              maxLength={1000}
            />
          </div>

          <div className="inner-os-ephemeral-row">
            <label htmlFor="inner-os-background" className="inner-os-ephemeral-label-inline">
              <MaskIcon size={12} />
              <span>私密底线</span>
            </label>
            <input
              id="inner-os-background"
              type="text"
              className="inner-os-ephemeral-input-inline"
              placeholder="坚守的底线或不便明言的顾虑 (如: 人力已饱和，不可妥协提前交付)"
              value={context.background || ""}
              onChange={(e) => onChange({ ...context, background: e.target.value })}
              maxLength={2000}
            />
          </div>

          <div className="inner-os-ephemeral-footer">
            <span className="inner-os-ephemeral-tip"><BroomIcon size={13} /> 仅在会期内作为私密参考，会议结束自动销毁，不留任何痕迹。</span>
            <div className="inner-os-ephemeral-actions">
              {hasContent && (
                <button
                  type="button"
                  className="inner-os-ephemeral-clear-btn"
                  onClick={onClear}
                >
                  清空
                </button>
              )}
              <button
                type="button"
                className="inner-os-ephemeral-save-btn"
                onClick={() => setIsOpen(false)}
              >
                完成
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
