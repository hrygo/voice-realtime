import React, { useEffect, useState } from "react";
import { useInnerOSStore } from "./innerOSStore";
import { InnerOSAnswerCard } from "./InnerOSAnswerCard";

interface Props {
  readonly meetingId: string;
  readonly onSelectEvidence?: (segmentId: string) => void;
}

export const InnerOSHistoryTab: React.FC<Props> = ({
  meetingId,
  onSelectEvidence,
}) => {
  const historyList = useInnerOSStore((s) => s.historyList);
  const isLoading = useInnerOSStore((s) => s.isLoadingHistory);
  const fetchHistory = useInnerOSStore((s) => s.fetchHistory);
  const deleteExchangeAction = useInnerOSStore((s) => s.deleteExchangeAction);

  const [deletingId, setDeletingId] = useState<string | null>(null);

  useEffect(() => {
    if (meetingId) {
      fetchHistory(meetingId);
    }
  }, [meetingId, fetchHistory]);

  const handleDelete = async (exchangeId: string) => {
    if (!window.confirm("确定要从会议档案中删除该条内心 OS 记录吗？")) return;
    setDeletingId(exchangeId);
    try {
      await deleteExchangeAction(meetingId, exchangeId);
    } finally {
      setDeletingId(null);
    }
  };

  if (isLoading && historyList.length === 0) {
    return (
      <div className="inner-os-history-loading">
        <span className="inner-os-spinner" />
        <span>正在加载内心 OS 档案...</span>
      </div>
    );
  }

  if (historyList.length === 0) {
    return (
      <div className="inner-os-history-empty">
        <span className="inner-os-empty-icon">🔒</span>
        <div className="inner-os-empty-title">暂无保存的内心 OS 记录</div>
        <div className="inner-os-empty-desc">
          在会议进行期间向内心 OS 提问并点击“保存此条问答”，即可在此归档并永久回溯。
        </div>
      </div>
    );
  }

  return (
    <div className="inner-os-history-tab" data-testid="inner-os-history-tab">
      <div className="inner-os-history-meta-bar">
        <span>共有 {historyList.length} 条已保存的问答档案</span>
      </div>

      <div className="inner-os-history-list">
        {historyList.map((exchange) => {
          const isDeleting = deletingId === exchange.id;
          return (
            <div key={exchange.id} className="inner-os-history-item-wrap">
              <div className="inner-os-history-item-header">
                <div className="inner-os-history-status-flags">
                  {exchange.evidence_invalidated ? (
                    <span className="inner-os-status-pill pill-danger">
                      🔴 原证据段已变更/修正
                    </span>
                  ) : exchange.context_advanced ? (
                    <span className="inner-os-status-pill pill-warning">
                      🟡 会议后续有新讨论
                    </span>
                  ) : (
                    <span className="inner-os-status-pill pill-success">
                      🟢 证据有效
                    </span>
                  )}
                  <span className="inner-os-history-time">
                    {new Date(exchange.created_at).toLocaleTimeString()}
                  </span>
                </div>
                <button
                  type="button"
                  className="inner-os-history-del-btn"
                  onClick={() => handleDelete(exchange.id)}
                  disabled={isDeleting}
                  title="从档案中删除"
                >
                  {isDeleting ? "删除中..." : "🗑️ 删除"}
                </button>
              </div>

              <InnerOSAnswerCard
                queryId={exchange.id}
                question={exchange.question}
                intent={exchange.intent}
                answer={exchange.answer}
                saved={true}
                onSave={() => {}}
                onSelectEvidence={onSelectEvidence}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
};
