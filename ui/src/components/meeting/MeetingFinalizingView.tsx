import { useEffect, useState } from "react";

const STAGES = [
  {
    id: 1,
    title: "冲刷尾部语音与说话人对齐",
    desc: "向 WhisperLiveKit 发送 EOF 帧，处理最后音频并完成 Sortformer 对齐",
    icon: "🎙️",
  },
  {
    id: 2,
    title: "规整转录段落与封存数据库",
    desc: "提交 PostgreSQL 事务，重新规整段落序号并记录结束时间",
    icon: "💾",
  },
  {
    id: 3,
    title: "排队唤醒 AI 纪要引擎",
    desc: "准备转录文本，由本地大模型（Qwen3.8-27b）异步生成结构化纪要",
    icon: "✨",
  },
] as const;

export function MeetingFinalizingView() {
  const [elapsed, setElapsed] = useState(0);
  const [activeStage, setActiveStage] = useState(1);

  useEffect(() => {
    const timer = setInterval(() => {
      setElapsed((prev) => {
        const next = prev + 1;
        if (next >= 4 && activeStage < 3) {
          setActiveStage(3);
        } else if (next >= 2 && activeStage < 2) {
          setActiveStage(2);
        }
        return next;
      });
    }, 1000);

    return () => clearInterval(timer);
  }, [activeStage]);

  return (
    <div className="finalizing-view">
      <div className="finalizing-badge">
        <span className="pulse-dot" />
        <span>会话收尾中 · 已进行 {elapsed}s (最长等待 8s 自动封存)</span>
      </div>

      <div className="finalizing-spinner-wrap">
        <div className="spinner-lg" />
        <div className="spinner-center-icon">
          {activeStage === 1 ? "🎙️" : activeStage === 2 ? "💾" : "✨"}
        </div>
      </div>

      <h3 className="finalizing-title">
        正在冲刷并封存会议记录...
      </h3>
      <p className="finalizing-subtitle">
        系统正在进行数据最终对账，请稍候片刻，即将为您自动打开会议详情与 AI 纪要工作区。
      </p>

      <div className="finalizing-steps-card">
        {STAGES.map((stage) => {
          const isDone = activeStage > stage.id;
          const isCurrent = activeStage === stage.id;

          return (
            <div
              key={stage.id}
              className={`finalizing-step-item ${isDone ? "done" : isCurrent ? "current" : "pending"}`}
            >
              <div className="step-icon-wrap">
                {isDone ? (
                  <span className="step-check">✓</span>
                ) : (
                  <span className="step-icon">{stage.icon}</span>
                )}
              </div>
              <div className="step-content">
                <div className="step-header">
                  <span className="step-title">{stage.title}</span>
                  {isCurrent && <span className="step-tag-active">处理中</span>}
                  {isDone && <span className="step-tag-done">已就绪</span>}
                </div>
                <div className="step-desc">{stage.desc}</div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
