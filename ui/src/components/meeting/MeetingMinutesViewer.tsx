import { useState } from "react";
import type {
  MeetingMinutesVersion,
  MinutesStatus,
} from "../../contracts/meetingContract";
import { getErrorMessageByCode } from "../../contracts/meetingContract";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { showToast } from "../Toast";
import { copyTextToClipboard } from "../../utils/clipboard";

interface MeetingMinutesViewerProps {
  minutes: MeetingMinutesVersion | null;
  minutesList: readonly MeetingMinutesVersion[];
  selectedVersion: number | null;
  onSelectVersion: (version: number) => void;
  onRegenerate: () => Promise<void>;
  onSelectEvidence: (segmentId: string) => void;
  isRegenerating: boolean;
  hideTitle?: boolean;
}

export function MeetingMinutesViewer({
  minutes,
  minutesList,
  selectedVersion,
  onSelectVersion,
  onRegenerate,
  onSelectEvidence,
  isRegenerating,
  hideTitle = false,
}: MeetingMinutesViewerProps) {
  const [viewMode, setViewMode] = useState<"structured" | "markdown">("structured");

  const status: MinutesStatus | null = minutes?.status || null;
  const isGenerating = isRegenerating || status === "queued" || status === "generating";
  const isFailed = status === "failed";
  const jsonContent = minutes?.content_json;

  const [completedItems, setCompletedItems] = useState<Set<number>>(() => {
    if (!minutes?.id) return new Set();
    try {
      const raw = localStorage.getItem(`voice-studio:action-items:${minutes.id}`);
      return raw ? new Set<number>(JSON.parse(raw)) : new Set<number>();
    } catch {
      return new Set<number>();
    }
  });

  const toggleActionItem = (idx: number) => {
    setCompletedItems((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      if (minutes?.id) {
        try {
          localStorage.setItem(`voice-studio:action-items:${minutes.id}`, JSON.stringify(Array.from(next)));
        } catch {
          // Ignore
        }
      }
      return next;
    });
  };

  const handleCopyChecklist = async () => {
    if (!jsonContent?.action_items?.length) return;
    const text = jsonContent.action_items
      .map(
        (item, i) =>
          `${completedItems.has(i) ? "- [x]" : "- [ ]"} ${item.task}${item.owner ? ` (@${item.owner})` : ""}${item.due_date ? ` (截止: ${item.due_date})` : ""}`,
      )
      .join("\n");
    try {
      await copyTextToClipboard(text);
      showToast("待办事项 Checklist 已成功复制", "success");
    } catch {
      showToast("复制失败，请检查浏览器剪贴板权限", "warning");
    }
  };

  const handleCopyMarkdown = async () => {
    if (!minutes?.content_markdown) return;
    try {
      await copyTextToClipboard(minutes.content_markdown);
      showToast("Markdown 纪要已复制到剪贴板", "success");
    } catch {
      showToast("复制失败，请检查浏览器剪贴板权限", "warning");
    }
  };

  return (
    <div className="minutes-pane">
      <div className="pane-header">
        <div className="pane-title-group">
          {!hideTitle && (
            <>
              <span className="pane-icon">✨</span>
              <span className="pane-title">AI 结构化纪要</span>
            </>
          )}
          {minutesList.length > 1 && (
            <select
              className="version-select"
              value={selectedVersion || minutes?.version || 1}
              onChange={(e) => onSelectVersion(Number(e.target.value))}
            >
              {minutesList.map((m) => (
                <option key={m.version} value={m.version}>
                  版本 {m.version} {m.is_stale ? "(已过期)" : ""}
                </option>
              ))}
            </select>
          )}
        </div>

        <div className="pane-actions-group">
          <button
            type="button"
            className="pane-header-btn"
            onClick={() => setViewMode((m) => (m === "structured" ? "markdown" : "structured"))}
          >
            <span>{viewMode === "structured" ? "📄" : "📊"}</span>
            <span>{viewMode === "structured" ? "Markdown 源码" : "结构化视图"}</span>
          </button>
          <button
            type="button"
            className={`pane-header-btn primary ${isGenerating ? "loading" : ""}`}
            onClick={() => void onRegenerate()}
            disabled={isGenerating}
          >
            <span>{isGenerating ? "⏳" : minutes ? "🔄" : "✨"}</span>
            <span>{isGenerating ? "生成中..." : minutes ? "重新生成" : "生成纪要"}</span>
          </button>
        </div>
      </div>

      <div className="minutes-body">
        {minutes?.is_stale && (
          <div className="stale-banner">
            <span>⚠️ 说话人名称或转录内容在此纪要生成后发生变更，当前内容可能过时。</span>
            <button
              type="button"
              className="pane-header-btn"
              style={{ fontSize: "0.68rem", padding: "2px 6px" }}
              onClick={() => void onRegenerate()}
              disabled={isGenerating}
            >
              立即重新生成
            </button>
          </div>
        )}

        {!minutes && !isGenerating && (
          <div className="minutes-card empty-state" style={{ textAlign: "center", padding: "36px 16px" }}>
            <div style={{ fontSize: "2.4rem", marginBottom: "10px" }}>✨</div>
            <h4 style={{ fontSize: "1rem", color: "var(--text-primary)", marginBottom: "6px" }}>
              尚未生成 AI 结构化纪要
            </h4>
            <p style={{ fontSize: "0.82rem", color: "var(--text-secondary)", marginBottom: "18px", maxWidth: "360px", margin: "0 auto 18px" }}>
              已封存会议转录可随时提取核心议题、决议事项、待办行动项及证据追溯。
            </p>
            <button
              type="button"
              className="btn-primary"
              style={{ padding: "8px 20px", fontSize: "0.85rem" }}
              onClick={() => void onRegenerate()}
            >
              ✨ 立即生成 AI 纪要
            </button>
          </div>
        )}

        {isGenerating && (
          <div className="minutes-generating-card">
            <div className="ai-generating-pulse-ring">
              <div className="spinner-ai" />
              <span className="ai-center-sparkle">✨</span>
            </div>
            <h4 className="ai-generating-title">
              正在提取并生成结构化 AI 纪要...
            </h4>
            <p className="ai-generating-desc">
              正在由本地大模型深度理解议题、决议、待办行动项及证据链
            </p>
            <div className="ai-steps-flow">
              <span className="ai-step-pill active">① 转录分段对齐</span>
              <span className="ai-step-arrow">➔</span>
              <span className="ai-step-pill active">② 核心议题提炼</span>
              <span className="ai-step-arrow">➔</span>
              <span className="ai-step-pill active">③ 决议与待办归纳</span>
              <span className="ai-step-arrow">➔</span>
              <span className="ai-step-pill">④ 证据链锚定</span>
            </div>
            <div className="ai-generating-footer">
              <span className="ai-model-tag">
                🤖 模型: {minutes?.model || "local/kat-coder-2.5"}
              </span>
              <span className="ai-safe-tip">
                🛡️ 本地推理 · 左侧转录可独立查看与导出
              </span>
            </div>
          </div>
        )}

        {isFailed && !isGenerating && (
          <div
            className="minutes-card"
            style={{
              borderColor: "var(--color-red)",
              background: "rgba(239, 68, 68, 0.08)",
            }}
          >
            <div className="minutes-card-title" style={{ color: "var(--color-red)" }}>
              <span>✕ AI 纪要生成失败</span>
            </div>
            <p style={{ fontSize: "0.82rem", color: "var(--text-secondary)", margin: "6px 0 10px 0" }}>
              {minutes?.error_message ||
                getErrorMessageByCode(minutes?.error_code || "summary_unavailable")}
            </p>
            <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
              <button
                type="button"
                className="btn-primary"
                style={{ fontSize: "0.78rem", padding: "4px 12px" }}
                onClick={() => void onRegenerate()}
              >
                🔄 重试生成
              </button>
            </div>
            <p style={{ fontSize: "0.72rem", color: "var(--text-tertiary)", marginTop: "8px" }}>
              提示：请确保 LLM 服务 (LM Studio) 在 localhost:1234 正常运行且已加载纪要模型。
            </p>
          </div>
        )}

        {!isGenerating && !isFailed && jsonContent && viewMode === "structured" && (
          <>
            {/* 0. 纪要主题提炼 */}
            {jsonContent.title && (
              <div
                className="minutes-card"
                style={{
                  borderColor: "rgba(99, 102, 241, 0.25)",
                  background: "rgba(99, 102, 241, 0.05)",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: "6px", marginBottom: "4px" }}>
                  <span
                    style={{
                      fontSize: "0.72rem",
                      color: "var(--color-accent)",
                      fontWeight: 600,
                    }}
                  >
                    🏷️ AI 纪要主题提炼
                  </span>
                </div>
                <h3
                  style={{
                    fontSize: "1.02rem",
                    fontWeight: 700,
                    color: "var(--text-primary)",
                    margin: 0,
                  }}
                >
                  {jsonContent.title}
                </h3>
              </div>
            )}

            {/* 1. 会议概要 */}
            {jsonContent.overview && (
              <div className="minutes-card">
                <h4 className="minutes-card-title">
                  <span>📋</span>
                  <span>会议概要</span>
                </h4>
                <p style={{ fontSize: "0.85rem", lineHeight: 1.6, color: "var(--text-primary)" }}>
                  {jsonContent.overview}
                </p>
              </div>
            )}

            {/* 2. 核心议题 */}
            {jsonContent.topics && jsonContent.topics.length > 0 && (
              <div className="minutes-card">
                <h4 className="minutes-card-title">
                  <span>💡</span>
                  <span>核心议题 ({jsonContent.topics.length})</span>
                </h4>
                <div className="structured-list">
                  {jsonContent.topics.map((t, idx) => (
                    <div key={idx} className="structured-item">
                      <div className="item-main">
                        <strong style={{ color: "var(--color-accent-light)" }}>
                          {idx + 1}. {t.title}
                        </strong>
                      </div>
                      <p style={{ color: "var(--text-secondary)", margin: "2px 0 4px 0" }}>
                        {t.summary}
                      </p>
                      {t.evidence_segment_ids?.length > 0 && (
                        <div className="item-meta-tags">
                          <span style={{ fontSize: "0.68rem", color: "var(--text-muted)" }}>
                            转录证据:
                          </span>
                          {t.evidence_segment_ids.map((id, i) => (
                            <button
                              key={id}
                              type="button"
                              className="evidence-pill"
                              onClick={() => onSelectEvidence(id)}
                              title="点击在左侧转录流中定位并高亮此段落"
                            >
                              <span>📌</span>
                              <span>证据 #{i + 1}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 3. 决策事项 */}
            {jsonContent.decisions && jsonContent.decisions.length > 0 && (
              <div className="minutes-card">
                <h4 className="minutes-card-title">
                  <span>✅</span>
                  <span>决策事项 ({jsonContent.decisions.length})</span>
                </h4>
                <div className="structured-list">
                  {jsonContent.decisions.map((d, idx) => (
                    <div key={idx} className="structured-item">
                      <div className="item-main">
                        <span>●</span>
                        <span>{d.content}</span>
                      </div>
                      {d.evidence_segment_ids?.length > 0 && (
                        <div className="item-meta-tags">
                          {d.evidence_segment_ids.map((id, i) => (
                            <button
                              key={id}
                              type="button"
                              className="evidence-pill"
                              onClick={() => onSelectEvidence(id)}
                            >
                              <span>📌</span>
                              <span>段落证据 #{i + 1}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 4. 待办行动项 */}
            {jsonContent.action_items && jsonContent.action_items.length > 0 && (
              <div className="minutes-card">
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <h4 className="minutes-card-title" style={{ margin: 0 }}>
                    <span>📌</span>
                    <span>待办行动项 ({jsonContent.action_items.length})</span>
                  </h4>
                  <button
                    type="button"
                    className="btn-secondary"
                    style={{ fontSize: "0.68rem", padding: "2px 6px" }}
                    onClick={() => void handleCopyChecklist()}
                    title="复制待办为标准 Markdown Checklist 格式"
                  >
                    📋 复制清单
                  </button>
                </div>
                <div className="structured-list">
                  {jsonContent.action_items.map((item, idx) => {
                    const isDone = completedItems.has(idx);
                    return (
                      <div key={idx} className={`structured-item ${isDone ? "item-completed" : ""}`}>
                        <div
                          className="item-main action-checkbox-row"
                          onClick={() => toggleActionItem(idx)}
                          style={{ cursor: "pointer", userSelect: "none" }}
                          title="点击标记完成/未完成"
                        >
                          <span className="action-check-box">{isDone ? "☑️" : "☐"}</span>
                          <strong
                            style={{
                              color: isDone ? "var(--text-muted)" : "var(--text-primary)",
                              textDecoration: isDone ? "line-through" : "none",
                            }}
                          >
                            {item.task}
                          </strong>
                        </div>
                        <div className="item-meta-tags">
                          {item.owner && <span className="owner-tag">👤 {item.owner}</span>}
                          {item.due_date && <span className="due-tag">📅 {item.due_date}</span>}
                          {item.evidence_segment_ids?.map((id, i) => (
                            <button
                              key={id}
                              type="button"
                              className="evidence-pill"
                              onClick={() => onSelectEvidence(id)}
                            >
                              <span>📌</span>
                              <span>证据 #{i + 1}</span>
                            </button>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* 5. 风险提示 */}
            {jsonContent.risks && jsonContent.risks.length > 0 && (
              <div className="minutes-card">
                <h4 className="minutes-card-title" style={{ color: "var(--color-yellow)" }}>
                  <span>⚠️</span>
                  <span>风险提示 ({jsonContent.risks.length})</span>
                </h4>
                <div className="structured-list">
                  {jsonContent.risks.map((r, idx) => (
                    <div key={idx} className="structured-item">
                      <div className="item-main">
                        <span>▲</span>
                        <span>{r.content}</span>
                      </div>
                      {r.evidence_segment_ids?.length > 0 && (
                        <div className="item-meta-tags">
                          {r.evidence_segment_ids.map((id, i) => (
                            <button
                              key={id}
                              type="button"
                              className="evidence-pill"
                              onClick={() => onSelectEvidence(id)}
                            >
                              <span>📌</span>
                              <span>证据 #{i + 1}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 6. 待定问题 */}
            {jsonContent.open_questions && jsonContent.open_questions.length > 0 && (
              <div className="minutes-card">
                <h4 className="minutes-card-title">
                  <span>❓</span>
                  <span>待定问题 ({jsonContent.open_questions.length})</span>
                </h4>
                <div className="structured-list">
                  {jsonContent.open_questions.map((q, idx) => (
                    <div key={idx} className="structured-item">
                      <div className="item-main">
                        <span>?</span>
                        <span>{q.content}</span>
                      </div>
                      {q.evidence_segment_ids?.length > 0 && (
                        <div className="item-meta-tags">
                          {q.evidence_segment_ids.map((id, i) => (
                            <button
                              key={id}
                              type="button"
                              className="evidence-pill"
                              onClick={() => onSelectEvidence(id)}
                            >
                              <span>📌</span>
                              <span>证据 #{i + 1}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 7. 精彩亮点 */}
            {jsonContent.highlights && jsonContent.highlights.length > 0 && (
              <div className="minutes-card">
                <h4 className="minutes-card-title">
                  <span>🌟</span>
                  <span>精彩亮点 ({jsonContent.highlights.length})</span>
                </h4>
                <div className="structured-list">
                  {jsonContent.highlights.map((h, idx) => (
                    <div key={idx} className="structured-item">
                      <div className="item-main">
                        <span>★</span>
                        <span>{h.content}</span>
                      </div>
                      {h.evidence_segment_ids?.length > 0 && (
                        <div className="item-meta-tags">
                          {h.evidence_segment_ids.map((id, i) => (
                            <button
                              key={id}
                              type="button"
                              className="evidence-pill"
                              onClick={() => onSelectEvidence(id)}
                            >
                              <span>📌</span>
                              <span>证据 #{i + 1}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}

        {/* Markdown 源码模式 */}
        {!isGenerating && !isFailed && viewMode === "markdown" && (
          <div className="minutes-card">
            <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: "8px" }}>
              <button
                type="button"
                className="btn-secondary"
                style={{ fontSize: "0.72rem", padding: "2px 8px" }}
                onClick={() => void handleCopyMarkdown()}
              >
                📋 复制 Markdown
              </button>
            </div>
            <MarkdownRenderer content={minutes?.content_markdown || ""} />
          </div>
        )}
      </div>
    </div>
  );
}
