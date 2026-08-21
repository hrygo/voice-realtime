import { useState } from "react";
import type {
  MeetingMinutesVersion,
  MinutesStatus,
} from "../../contracts/meetingContract";
import { getErrorMessageByCode } from "../../contracts/meetingContract";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { showToast } from "../Toast";

interface MeetingMinutesViewerProps {
  minutes: MeetingMinutesVersion | null;
  minutesList: readonly MeetingMinutesVersion[];
  selectedVersion: number | null;
  onSelectVersion: (version: number) => void;
  onRegenerate: () => Promise<void>;
  onSelectEvidence: (segmentId: string) => void;
  isRegenerating: boolean;
}

export function MeetingMinutesViewer({
  minutes,
  minutesList,
  selectedVersion,
  onSelectVersion,
  onRegenerate,
  onSelectEvidence,
  isRegenerating,
}: MeetingMinutesViewerProps) {
  const [viewMode, setViewMode] = useState<"structured" | "markdown">("structured");

  const status: MinutesStatus = minutes?.status || "queued";
  const isGenerating = status === "queued" || status === "generating" || isRegenerating;
  const isFailed = status === "failed";
  const jsonContent = minutes?.content_json;

  return (
    <div className="minutes-pane">
      <div className="pane-header">
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <span>✨ AI 结构化纪要</span>
          {minutesList.length > 1 && (
            <select
              className="speaker-select"
              style={{ padding: "2px 6px", fontSize: "0.72rem" }}
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

        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <button
            type="button"
            className="btn-secondary"
            style={{ fontSize: "0.72rem", padding: "2px 8px" }}
            onClick={() => setViewMode((m) => (m === "structured" ? "markdown" : "structured"))}
          >
            {viewMode === "structured" ? "📄 Markdown 源码" : "📊 结构化视图"}
          </button>
          <button
            type="button"
            className="btn-secondary"
            style={{ fontSize: "0.72rem", padding: "2px 8px" }}
            onClick={() => void onRegenerate()}
            disabled={isGenerating}
          >
            {isGenerating ? "生成中..." : "🔄 重新生成"}
          </button>
        </div>
      </div>

      <div className="minutes-body">
        {minutes?.is_stale && (
          <div className="stale-banner">
            <span>⚠️ 说话人名称或转录内容在此纪要生成后发生变更，当前内容可能过时。</span>
            <button
              type="button"
              className="btn-secondary"
              style={{ fontSize: "0.68rem", padding: "2px 6px" }}
              onClick={() => void onRegenerate()}
              disabled={isGenerating}
            >
              立即重新生成
            </button>
          </div>
        )}

        {isGenerating && (
          <div className="minutes-card" style={{ textAlign: "center", padding: "32px 16px" }}>
            <div className="spinner-lg" style={{ margin: "0 auto 12px" }} />
            <h4 style={{ fontSize: "0.95rem", color: "var(--text-primary)" }}>
              正在提取并生成结构化 AI 纪要...
            </h4>
            <p style={{ fontSize: "0.78rem", color: "var(--text-secondary)", marginTop: "4px" }}>
              模型：{minutes?.model || "qwen/qwen3.8-27b"} (已封存转录可独立查看)
            </p>
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
            <p style={{ fontSize: "0.82rem", color: "var(--text-secondary)" }}>
              {minutes?.error_message ||
                getErrorMessageByCode(minutes?.error_code || "summary_unavailable")}
            </p>
            <button
              type="button"
              className="btn-secondary"
              style={{ alignSelf: "flex-start", marginTop: "8px" }}
              onClick={() => void onRegenerate()}
            >
              重试生成
            </button>
          </div>
        )}

        {!isGenerating && !isFailed && jsonContent && viewMode === "structured" && (
          <>
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
                <h4 className="minutes-card-title">
                  <span>📌</span>
                  <span>待办行动项 ({jsonContent.action_items.length})</span>
                </h4>
                <div className="structured-list">
                  {jsonContent.action_items.map((item, idx) => (
                    <div key={idx} className="structured-item">
                      <div className="item-main">
                        <span>☐</span>
                        <strong style={{ color: "var(--text-primary)" }}>{item.task}</strong>
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
                  ))}
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
                onClick={async () => {
                  if (minutes?.content_markdown) {
                    await navigator.clipboard.writeText(minutes.content_markdown);
                    showToast("Markdown 纪要已复制到剪贴板", "success");
                  }
                }}
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
