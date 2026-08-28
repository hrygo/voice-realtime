import { useEffect, useRef, useState } from "react";

export interface MeetingEndConfirmModalProps {
  isOpen: boolean;
  meetingTitle: string;
  elapsedLabel: string;
  segmentCount: number;
  isConfirming: boolean;
  onClose: () => void;
  onConfirm: () => Promise<boolean>;
}

export function MeetingEndConfirmModal({
  isOpen,
  meetingTitle,
  elapsedLabel,
  segmentCount,
  isConfirming,
  onClose,
  onConfirm,
}: MeetingEndConfirmModalProps) {
  const continueButtonRef = useRef<HTMLButtonElement>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const isBusy = isConfirming || isSubmitting;

  useEffect(() => {
    if (isOpen) {
      continueButtonRef.current?.focus();
    }
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !isBusy) {
        event.preventDefault();
        onClose();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, isBusy, onClose]);

  if (!isOpen) return null;

  const handleConfirm = async () => {
    if (isBusy) return;

    setIsSubmitting(true);
    try {
      if (await onConfirm()) {
        onClose();
      }
    } catch {
      // The parent owns error feedback; keeping the dialog open is the safe fallback.
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div
      className="modal-backdrop meeting-end-confirm-backdrop"
      onClick={() => {
        if (!isBusy) onClose();
      }}
    >
      <section
        className="modal-dialog meeting-end-confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="meeting-end-confirm-title"
        aria-describedby="meeting-end-confirm-description"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="meeting-end-confirm-header">
          <div className="meeting-end-confirm-icon" aria-hidden="true">
            <span>⏹</span>
          </div>
          <div className="meeting-end-confirm-heading">
            <span className="meeting-end-confirm-eyebrow">结束会议</span>
            <h2 id="meeting-end-confirm-title">要现在结束这场会议吗？</h2>
          </div>
          <button
            type="button"
            className="meeting-end-confirm-close"
            aria-label="关闭结束会议确认"
            onClick={onClose}
            disabled={isBusy}
          >
            ×
          </button>
        </div>

        <div className="meeting-end-confirm-session">
          <div className="meeting-end-confirm-session-title">
            <span className="meeting-end-confirm-live-dot" aria-hidden="true" />
            <div>
              <span className="meeting-end-confirm-label">当前会议</span>
              <strong title={meetingTitle}>{meetingTitle}</strong>
            </div>
          </div>
          <div className="meeting-end-confirm-metrics" aria-label="当前会议统计">
            <div>
              <strong>{elapsedLabel}</strong>
              <span>录制时长</span>
            </div>
            <div>
              <strong>{segmentCount}</strong>
              <span>已确认片段</span>
            </div>
          </div>
        </div>

        <div className="meeting-end-confirm-body">
          <p id="meeting-end-confirm-description" className="meeting-end-confirm-description">
            结束后系统会停止收音、冲刷最后一段转录并封存会议记录。已保存的内容不会丢失，AI 纪要将在后台排队生成。
          </p>
          <div
            className="meeting-end-confirm-effects"
            role="list"
            aria-label="结束会议后将执行的操作"
          >
            <div className="meeting-end-confirm-effect" role="listitem">
              <span className="meeting-end-confirm-effect-index">1</span>
              <div>
                <strong>停止收音</strong>
                <span>不再接收新的麦克风音频</span>
              </div>
            </div>
            <div className="meeting-end-confirm-effect" role="listitem">
              <span className="meeting-end-confirm-effect-index">2</span>
              <div>
                <strong>冲刷并封存</strong>
                <span>对齐尾部发言，保存到会议历史</span>
              </div>
            </div>
            <div className="meeting-end-confirm-effect" role="listitem">
              <span className="meeting-end-confirm-effect-index">3</span>
              <div>
                <strong>生成 AI 纪要</strong>
                <span>后台处理完成后可在详情页查看</span>
              </div>
            </div>
          </div>
        </div>

        <div className="meeting-end-confirm-actions">
          <button
            ref={continueButtonRef}
            type="button"
            className="meeting-end-confirm-cancel"
            onClick={onClose}
            disabled={isBusy}
          >
            继续录制
            <kbd>Esc</kbd>
          </button>
          <button
            type="button"
            className="meeting-end-confirm-submit"
            onClick={() => void handleConfirm()}
            disabled={isBusy}
            aria-busy={isBusy}
          >
            <span aria-hidden="true">{isBusy ? "◌" : "⏹"}</span>
            <span>{isBusy ? "正在封存…" : "结束并封存"}</span>
          </button>
        </div>
        <p className="meeting-end-confirm-footnote">结束后仍可在历史记录中查看和编辑会议详情。</p>
      </section>
    </div>
  );
}
