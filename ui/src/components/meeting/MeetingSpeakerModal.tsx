import { useState, useEffect } from "react";

interface MeetingSpeakerModalProps {
  isOpen: boolean;
  speakerKey: string;
  currentDisplayName: string;
  onClose: () => void;
  onSave: (speakerKey: string, newDisplayName: string) => Promise<void>;
}

export function MeetingSpeakerModal({
  isOpen,
  speakerKey,
  currentDisplayName,
  onClose,
  onSave,
}: MeetingSpeakerModalProps) {
  const [name, setName] = useState(currentDisplayName);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    setName(currentDisplayName);
  }, [currentDisplayName, isOpen]);

  if (!isOpen) return null;

  const handleSave = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    setIsSaving(true);
    try {
      await onSave(speakerKey, trimmed);
      onClose();
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3 className="modal-title">重命名说话人</h3>
          <button
            type="button"
            className="btn-secondary"
            onClick={onClose}
            style={{ padding: "2px 8px" }}
          >
            ✕
          </button>
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="speaker-name-input">
            说话人名称 (通道: {speakerKey})
          </label>
          <input
            id="speaker-name-input"
            className="form-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="输入说话人真实姓名或角色"
            autoFocus
            maxLength={50}
            onKeyDown={(e) => {
              if (e.key === "Enter") void handleSave();
              if (e.key === "Escape") onClose();
            }}
          />
          <div style={{ display: "flex", gap: "6px", flexWrap: "wrap", marginTop: "6px" }}>
            <span style={{ fontSize: "0.7rem", color: "var(--text-muted)", alignSelf: "center" }}>
              快捷预设:
            </span>
            {["🎤 主持人", "📊 汇报人", "💻 技术负责", "🎨 产品经理", "💼 决策人"].map((role) => (
              <button
                key={role}
                type="button"
                className="btn-secondary"
                style={{ fontSize: "0.68rem", padding: "1px 6px" }}
                onClick={() => setName(role.replace(/^.. /, ""))}
              >
                {role}
              </button>
            ))}
          </div>
        </div>

        <div className="modal-actions">
          <button
            type="button"
            className="btn-secondary"
            onClick={onClose}
            disabled={isSaving}
          >
            取消
          </button>
          <button
            type="button"
            className="btn-new-meeting"
            onClick={() => void handleSave()}
            disabled={isSaving || !name.trim()}
          >
            {isSaving ? "保存中..." : "保存修改"}
          </button>
        </div>
      </div>
    </div>
  );
}
