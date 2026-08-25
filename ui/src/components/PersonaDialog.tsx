import type { PersonaTemplate } from "../stores/uiSettingsStore";
import { showToast } from "./Toast";
import "./PersonaDialog.css";

interface PersonaDialogProps {
  readonly templates: readonly PersonaTemplate[];
  readonly draft: string;
  readonly error: string;
  readonly showAddCustom: boolean;
  readonly newTemplateName: string;
  readonly commandReady: boolean;
  readonly onDraftChange: (value: string) => void;
  readonly onClearError: () => void;
  readonly onToggleAddCustom: () => void;
  readonly onNewTemplateNameChange: (value: string) => void;
  readonly onSaveCustom: () => void;
  readonly onRemoveCustom: (id: string) => void;
  readonly onCancel: () => void;
  readonly onSave: () => Promise<void>;
}

export function PersonaDialog({
  templates,
  draft,
  error,
  showAddCustom,
  newTemplateName,
  commandReady,
  onDraftChange,
  onClearError,
  onToggleAddCustom,
  onNewTemplateNameChange,
  onSaveCustom,
  onRemoveCustom,
  onCancel,
  onSave,
}: PersonaDialogProps) {
  return (
    <div
      className="persona-modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="persona-modal-title"
      onClick={onCancel}
    >
      <div className="persona-modal-dialog" onClick={(event) => event.stopPropagation()}>
        <div className="persona-dialog-header">
          <h3 id="persona-modal-title"><span>🎭</span> 助手人设库与提示词定制</h3>
          <button type="button" className="persona-dialog-close" onClick={onCancel} aria-label="关闭">
            ✕
          </button>
        </div>

        <div className="persona-dialog-body">
          <div className="persona-presets-section">
            <div className="presets-header-line">
              <span className="presets-label">人设模板库 (点击载入)</span>
              <button type="button" className="preset-chip" onClick={onToggleAddCustom}>
                {showAddCustom ? "取消新增" : "+ 存为新模板"}
              </button>
            </div>

            <div className="presets-chips">
              {templates.map((preset) => {
                const isSelected = draft.trim() === preset.prompt.trim();
                return (
                  <span
                    key={preset.id}
                    className={`preset-chip ${isSelected ? "active" : ""}`}
                    onClick={() => onDraftChange(preset.prompt)}
                  >
                    {preset.name}
                    {!preset.isBuiltin && (
                      <span
                        className="preset-delete-icon"
                        onClick={(event) => {
                          event.stopPropagation();
                          onRemoveCustom(preset.id);
                          showToast(`已删除人设: ${preset.name}`, "info");
                        }}
                        title="删除此自定义模板"
                      >
                        ✕
                      </span>
                    )}
                  </span>
                );
              })}
            </div>

            {showAddCustom && (
              <div className="persona-custom-creator">
                <input
                  type="text"
                  className="persona-custom-name-input"
                  placeholder="输入新模板名称 (如: 🎙️ 英语面试官)..."
                  value={newTemplateName}
                  onChange={(event) => onNewTemplateNameChange(event.target.value)}
                />
                <button type="button" className="btn-primary" onClick={onSaveCustom}>
                  保存模板
                </button>
              </div>
            )}
          </div>

          <div className="persona-textarea-wrap">
            <textarea
              className="persona-textarea"
              value={draft}
              onChange={(event) => {
                onDraftChange(event.target.value);
                if (error) onClearError();
              }}
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                  event.preventDefault();
                  void onSave();
                }
              }}
              placeholder="输入助手 System Prompt 提示词..."
              rows={8}
              aria-label="系统提示词"
            />
            <div className="persona-meta-bar">
              <span>支持快捷键 <kbd>Cmd / Ctrl + Enter</kbd> 快速保存</span>
              <span>{draft.length} 字符</span>
            </div>
            {error && <p className="persona-error">{error}</p>}
          </div>
        </div>

        <div className="persona-dialog-footer">
          <span style={{ fontSize: "0.74rem", color: "var(--text-muted)" }}>
            保存后立即向 LM Studio 下发并清空当前上下文
          </span>
          <div className="persona-dialog-footer-right">
            <button type="button" className="btn-ctrl" onClick={onCancel}>取消</button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void onSave()}
              disabled={!commandReady}
            >
              应用并生效
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
