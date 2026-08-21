export function MeetingFinalizingView() {
  return (
    <div className="finalizing-view">
      <div className="spinner-lg" />
      <h3 style={{ fontSize: "1.1rem", fontWeight: 700, color: "var(--text-primary)" }}>
        正在冲刷并封存会议记录...
      </h3>
      <p style={{ fontSize: "0.85rem", color: "var(--text-secondary)", maxWidth: "440px" }}>
        向 WhisperLiveKit 发送结束信号，正在等待最后一段语音转写对账并提交数据库事务。封存完成后将自动排队生成 AI 纪要。
      </p>
    </div>
  );
}
