import "./ShortcutsModal.css";

interface ShortcutsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

const SHORTCUTS = [
  { key: "Cmd / Ctrl + 1 / 2 / 3", desc: "快速切换「语音助手」/「会议助手」/「实时字幕」" },
  { key: "M", desc: "切换麦克风静音 / 恢复录音" },
  { key: "Cmd / Ctrl + K", desc: "打开 / 关闭人设提示词库" },
  { key: "Cmd / Ctrl + Shift + C", desc: "清空 LLM 上下文记忆" },
  { key: "Cmd / Ctrl + Shift + M", desc: "快速导出 Markdown 结构化会议纪要" },
  { key: "Cmd / Ctrl + Shift + S", desc: "快速导出 SRT 标准字幕" },
  { key: "Cmd / Ctrl + Shift + P", desc: "开启 / 退出舞台提词与大屏模式" },
  { key: "Cmd / Ctrl + Enter", desc: "在人设编辑器中快速保存并生效" },
  { key: "?", desc: "打开快捷键速查面板" },
  { key: "Esc", desc: "关闭弹窗 / 退出全屏提词大字模式" },
];

export default function ShortcutsModal({ isOpen, onClose }: ShortcutsModalProps) {
  if (!isOpen) return null;

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="shortcuts-title"
      onClick={onClose}
    >
      <div className="modal-content shortcuts-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title-wrap">
            <span className="modal-title-icon">⌨️</span>
            <h3 id="shortcuts-title">Voice Studio 快捷键速查</h3>
          </div>
          <button
            type="button"
            className="modal-close-btn"
            onClick={onClose}
            aria-label="关闭"
          >
            ✕
          </button>
        </div>

        <div className="shortcuts-list">
          {SHORTCUTS.map((item, idx) => (
            <div className="shortcut-row" key={idx}>
              <span className="shortcut-desc">{item.desc}</span>
              <kbd className="shortcut-key">{item.key}</kbd>
            </div>
          ))}
        </div>

        <div className="modal-footer">
          <span className="shortcut-tip">随时按 <kbd>Esc</kbd> 退出</span>
          <button type="button" className="btn-primary" onClick={onClose}>
            我知道了
          </button>
        </div>
      </div>
    </div>
  );
}
