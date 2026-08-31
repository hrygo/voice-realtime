import { useState } from "react";
import type { MeetingHealthState } from "../../stores/meetingStore";
import { MeetingWaveform } from "./MeetingWaveform";

interface MeetingIdleViewProps {
  health: MeetingHealthState;
  micMuted: boolean;
  onStartMeeting: (title: string, maxSpeakers?: number) => Promise<void>;
  isStarting: boolean;
}

export function MeetingIdleView({
  health,
  micMuted,
  onStartMeeting,
  isStarting,
}: MeetingIdleViewProps) {
  const defaultTitle = `会议纪要 ${new Date().toLocaleDateString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  })}`;

  const [title, setTitle] = useState(defaultTitle);
  const [maxSpeakers, setMaxSpeakers] = useState<number>(4);

  const isStorageReady = health.storage === "ok" || health.storage === "degraded";
  const isTranscriptionReady = health.transcription === "ok";
  const canStart = isStorageReady && isTranscriptionReady && !isStarting;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canStart) return;
    void onStartMeeting(title.trim() || defaultTitle, maxSpeakers);
  };

  return (
    <div className="idle-view">
      <div className="idle-icon-hero">🎙️</div>
      <h2 className="idle-title">Voice Studio 会议助手</h2>
      <p className="idle-subtitle">
        全本地会议转录与 AI 纪要。会议期间完全停用语音助手对话与 TTS 播报，所有发言持续持久化至 PostgreSQL。
      </p>

      {/* 静息环境声学就绪波形 */}
      <div className="idle-waveform-wrap">
        <MeetingWaveform
          isRecording={false}
          hasPartial={false}
          isMuted={micMuted}
        />
      </div>


      <form className="idle-form-card" onSubmit={handleSubmit}>
        <div className="form-group">
          <label className="form-label" htmlFor="meeting-title-input">
            会议主题 / 标题
          </label>
          <input
            id="meeting-title-input"
            className="form-input"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="输入本次会议主题（1-200 字符）"
            maxLength={200}
            disabled={isStarting}
          />
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="meeting-speakers-select">
            预期发言人数（先验约束）
          </label>
          <select
            id="meeting-speakers-select"
            className="form-input"
            value={maxSpeakers}
            onChange={(e) => setMaxSpeakers(Number(e.target.value))}
            disabled={isStarting}
            style={{ cursor: "pointer" }}
          >
            <option value={1}>1 人（单人口述 / 独白总结）</option>
            <option value={2}>2 人（1v1 访谈 / 双人对话）</option>
            <option value={4}>3~4 人（标准多人会议讨论）</option>
          </select>
        </div>

        <div className="checklist-group">
          <div className="check-item">
            <span>🗄️ PostgreSQL 知识库存储 (voice_realtime)</span>
            <span
              className={`check-status-tag ${
                health.storage === "ok"
                  ? "ok"
                  : health.storage === "degraded"
                  ? "warn"
                  : "error"
              }`}
            >
              {health.storage === "ok"
                ? "● 就绪"
                : health.storage === "degraded"
                ? "● 降级（本地日志中）"
                : "✕ 不可用"}
            </span>
          </div>

          <div className="check-item">
            <span>🎙️ SpeechRail 实时转录服务</span>
            <span
              className={`check-status-tag ${
                health.transcription === "ok" ? "ok" : "error"
              }`}
            >
              {health.transcription === "ok" ? "● 就绪" : "✕ 服务未连接"}
            </span>
          </div>

          <div className="check-item">
            <span>👥 Sortformer 说话人分离 (Diarization)</span>
            <span className="check-status-tag ok">● 已启用 ({maxSpeakers}通道上限)</span>
          </div>

          <div className="check-item">
            <span>🎤 麦克风音频采集</span>
            <span className={`check-status-tag ${micMuted ? "warn" : "ok"}`}>
              {micMuted ? "● 麦克风已静音" : "● 16kHz 采集就绪"}
            </span>
          </div>
        </div>

        <button
          type="submit"
          className="btn-start-meeting"
          disabled={!canStart}
        >
          <span>🎙️</span>
          <span>{isStarting ? "正在初始化会议会话..." : "开始会议"}</span>
        </button>
      </form>
    </div>
  );
}
