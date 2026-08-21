import { useState } from "react";

interface MeetingDeleteModalProps {
  isOpen: boolean;
  meetingTitle: string;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}

export function MeetingDeleteModal({
  isOpen,
  meetingTitle,
  onClose,
  onConfirm,
}: MeetingDeleteModalProps) {
  const [isDeleting, setIsDeleting] = useState(false);

  if (!isOpen) return null;

  const handleDelete = async () => {
    setIsDeleting(true);
    try {
      await onConfirm();
      onClose();
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3 className="modal-title" style={{ color: "var(--color-red)" }}>
            删除会议记录
          </h3>
          <button
            type="button"
            className="btn-secondary"
            onClick={onClose}
            style={{ padding: "2px 8px" }}
          >
            ✕
          </button>
        </div>

        <p style={{ fontSize: "0.85rem", color: "var(--text-secondary)", lineHeight: 1.5 }}>
          确定要永久删除会议 <strong style={{ color: "var(--text-primary)" }}>“{meetingTitle}”</strong> 吗？
          此操作将级联删除该会议的所有转录段、说话人映射及 AI 纪要版本，且无法恢复。
        </p>

        <div className="modal-actions">
          <button
            type="button"
            className="btn-secondary"
            onClick={onClose}
            disabled={isDeleting}
          >
            取消
          </button>
          <button
            type="button"
            className="btn-end-meeting"
            onClick={() => void handleDelete()}
            disabled={isDeleting}
          >
            {isDeleting ? "删除中..." : "确认删除"}
          </button>
        </div>
      </div>
    </div>
  );
}
