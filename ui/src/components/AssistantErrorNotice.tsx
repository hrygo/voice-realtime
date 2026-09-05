import "./AssistantErrorNotice.css";

interface AssistantErrorNoticeProps {
  readonly message: string;
  readonly retryText?: string;
  readonly retrying?: boolean;
  readonly onRetry?: () => void;
}

export function AssistantErrorNotice({
  message,
  retryText,
  retrying = false,
  onRetry,
}: AssistantErrorNoticeProps) {
  const canRetry = Boolean(retryText?.trim()) && onRetry !== undefined;

  return (
    <section className="assistant-error-notice" role="alert" aria-live="assertive">
      <div className="assistant-error-notice-copy">
        <strong>助手暂时无法回复</strong>
        <p>{message}</p>
      </div>
      {canRetry && (
        <button
          type="button"
          className="assistant-error-retry"
          onClick={onRetry}
          disabled={retrying}
        >
          {retrying ? "正在准备…" : "重新编辑输入"}
        </button>
      )}
    </section>
  );
}
