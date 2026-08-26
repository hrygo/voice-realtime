export const mockMeetingSummaryRecording = {
  id: "8c314f50-c8c4-4a57-9a54-e12ab9bc237c",
  title: "Q3 架构技术方案评审会",
  status: "recording" as const,
  language: "Chinese",
  started_at: "2026-08-21T10:00:00Z",
  ended_at: null,
  transcript_revision: 5,
  content_revision: 5,
  interruption_reason: null,
  created_at: "2026-08-21T10:00:00Z",
};

export const mockMeetingSummaryCompleted = {
  id: "9d425a61-d9d5-5b68-ab65-f23bc0cd348d",
  title: "实时语音与字幕产品评审",
  status: "completed" as const,
  language: "Chinese",
  started_at: "2026-08-21T09:00:00Z",
  ended_at: "2026-08-21T09:45:00Z",
  transcript_revision: 42,
  content_revision: 43,
  interruption_reason: null,
  created_at: "2026-08-21T09:00:00Z",
};

export const mockMeetingSummaryInterrupted = {
  id: "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  title: "晨间快速同步会",
  status: "interrupted" as const,
  language: "Chinese",
  started_at: "2026-08-21T08:30:00Z",
  ended_at: "2026-08-21T08:42:00Z",
  transcript_revision: 12,
  content_revision: 12,
  interruption_reason: "finalization_timeout",
  created_at: "2026-08-21T08:30:00Z",
};

export const mockSegments = [
  {
    id: "seg-001-uuid",
    order: 1,
    speaker_key: "spk_channel_0",
    speaker_name: "张三 (架构师)",
    start_ms: 0,
    end_ms: 12450,
    text: "大家好，今天我们重点讨论会议助手的前端与后端契约设计。",
    translation: null,
    detected_language: "zh",
    source_epoch: 1,
  },
  {
    id: "seg-002-uuid",
    order: 2,
    speaker_key: "spk_channel_1",
    speaker_name: "李四 (前端负责人)",
    start_ms: 13000,
    end_ms: 24800,
    text: "前端部分将严格依据 v1 OpenAPI 和 WebSocket 契约独立实施。",
    translation: null,
    detected_language: "zh",
    source_epoch: 1,
  },
  {
    id: "seg-003-uuid",
    order: 3,
    speaker_key: "spk_channel_0",
    speaker_name: "张三 (架构师)",
    start_ms: 25100,
    end_ms: 38200,
    text: "好的，我们要确保滑动窗口对账算法准确无误，避免重复和丢失历史。",
    translation: null,
    detected_language: "zh",
    source_epoch: 1,
  },
  {
    id: "seg-004-uuid",
    order: 4,
    speaker_key: "spk_channel_2",
    speaker_name: "说话人 3",
    start_ms: 39000,
    end_ms: 51200,
    text: "我们计划在今天下班前完成全部单元测试与 Vitest 组件覆盖。",
    translation: null,
    detected_language: "zh",
    source_epoch: 1,
  },
];

export const mockMinutesCompleted = {
  id: "min-001-uuid",
  meeting_id: "9d425a61-d9d5-5b68-ab65-f23bc0cd348d",
  version: 1,
  status: "completed" as const,
  source_content_revision: 42,
  model: "qwen/qwen3.6-35b-a3b",
  prompt_version: "v1.0",
  content_json: {
    title: "实时语音与字幕产品评审",
    overview: "本次会议讨论了 Voice Studio 会议助手的前后端解耦实现方案，明确了契约标准和交付时间表。",
    topics: [
      {
        title: "前后端契约规范与解耦开发",
        summary: "前端将基于 OpenAPI 与 WebSocket v1 规范独立开发，不依赖后端具体内部代码。",
        evidence_segment_ids: ["seg-001-uuid", "seg-002-uuid"],
      },
      {
        title: "转录对账与一致性算法",
        summary: "确认采用基于 replace_from_ms 的滑动窗口对账算法保障数据持久化完整性。",
        evidence_segment_ids: ["seg-003-uuid"],
      },
    ],
    decisions: [
      {
        content: "严格按照 2026-08-21 设计规格推进前端交付，不改变单一所有者约束。",
        evidence_segment_ids: ["seg-002-uuid"],
      },
    ],
    action_items: [
      {
        task: "完成 Vitest 单元测试与 Fixtures 覆盖",
        owner: "李四",
        due_date: "2026-08-21",
        evidence_segment_ids: ["seg-004-uuid"],
      },
      {
        task: "准备真实的 Apple Silicon 并发联调环境",
        owner: "张三",
        due_date: "2026-08-22",
        evidence_segment_ids: ["seg-001-uuid"],
      },
    ],
    risks: [
      {
        content: "多说话人通道超过 4 人时可能出现声纹归属漂移。",
        evidence_segment_ids: ["seg-003-uuid"],
      },
    ],
    open_questions: [
      {
        content: "后续是否引入系统内部音频混合采集。",
        evidence_segment_ids: ["seg-001-uuid"],
      },
    ],
    highlights: [
      {
        content: "前后端双方对 V1 契约规范达成高度共识。",
        evidence_segment_ids: ["seg-002-uuid", "seg-004-uuid"],
      },
    ],
  },
  content_markdown: `# 会议纪要：实时语音与字幕产品评审\n\n## 概要\n本次会议讨论了 Voice Studio 会议助手的前后端解耦实现方案...\n`,
  raw_output: null,
  error_code: null,
  error_message: null,
  created_at: "2026-08-21T09:46:00Z",
  is_stale: false,
};

export const mockSpeakers = {
  spk_channel_0: {
    speaker_key: "spk_channel_0",
    original_speaker: "0",
    default_label: "说话人 1",
    display_name: "张三 (架构师)",
    updated_at: "2026-08-21T09:10:00Z",
  },
  spk_channel_1: {
    speaker_key: "spk_channel_1",
    original_speaker: "1",
    default_label: "说话人 2",
    display_name: "李四 (前端负责人)",
    updated_at: "2026-08-21T09:12:00Z",
  },
  spk_channel_2: {
    speaker_key: "spk_channel_2",
    original_speaker: "2",
    default_label: "说话人 3",
    display_name: "说话人 3",
    updated_at: "2026-08-21T09:00:00Z",
  },
};

export const mockMeetingDetailCompleted = {
  ...mockMeetingSummaryCompleted,
  audio_source: "microphone",
  interruption_reason: null,
  speakers: mockSpeakers,
  latest_minutes: mockMinutesCompleted,
  updated_at: "2026-08-21T09:46:00Z",
};

export const mockMeetingDetailRecording = {
  ...mockMeetingSummaryRecording,
  audio_source: "microphone",
  interruption_reason: null,
  speakers: mockSpeakers,
  latest_minutes: null,
  updated_at: "2026-08-21T10:00:00Z",
};
