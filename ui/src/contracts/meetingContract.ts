/**
 * Voice Studio 会议助手 V1 契约定义
 * 依据 docs/superpowers/specs/2026-08-21-meeting-assistant-design.md
 */

export type RuntimeMode = "assistant" | "subtitles" | "meeting" | "idle";

export type PCMOwner = "assistant" | "subtitles" | "meeting" | "none";

export type MeetingStatus =
  | "recording"
  | "finalizing"
  | "completed"
  | "interrupted"
  | "storage_error";

export type MinutesStatus = "queued" | "generating" | "completed" | "failed";

export type StorageHealth = "ok" | "degraded" | "unavailable";

export type ExportFormat = "md" | "txt" | "srt" | "json";

export interface TranscriptSegment {
  readonly id: string;
  readonly order: number;
  readonly speaker_key: string;
  readonly speaker_name: string;
  readonly start_ms: number;
  readonly end_ms: number;
  readonly text: string;
  readonly translation?: string | null;
  readonly detected_language?: string;
  readonly source_epoch?: number;
}

export interface MeetingSpeaker {
  readonly speaker_key: string;
  readonly original_speaker?: string;
  readonly default_label: string;
  readonly display_name: string;
  readonly updated_at: string;
}

export interface EvidenceTopic {
  readonly title: string;
  readonly summary: string;
  readonly evidence_segment_ids: string[];
}

export interface EvidenceDecision {
  readonly content: string;
  readonly evidence_segment_ids: string[];
}

export interface EvidenceActionItem {
  readonly task: string;
  readonly owner: string | null;
  readonly due_date: string | null;
  readonly evidence_segment_ids: string[];
}

export interface EvidenceRisk {
  readonly content: string;
  readonly evidence_segment_ids: string[];
}

export interface EvidenceQuestion {
  readonly content: string;
  readonly evidence_segment_ids: string[];
}

export interface EvidenceHighlight {
  readonly content: string;
  readonly evidence_segment_ids: string[];
}

export interface MinutesContentJson {
  readonly overview: string;
  readonly topics: EvidenceTopic[];
  readonly decisions: EvidenceDecision[];
  readonly action_items: EvidenceActionItem[];
  readonly risks: EvidenceRisk[];
  readonly open_questions: EvidenceQuestion[];
  readonly highlights: EvidenceHighlight[];
}

export interface MeetingMinutesVersion {
  readonly id: string;
  readonly meeting_id: string;
  readonly version: number;
  readonly status: MinutesStatus;
  readonly source_content_revision: number;
  readonly model: string;
  readonly prompt_version?: string;
  readonly content_json: MinutesContentJson | null;
  readonly content_markdown: string | null;
  readonly raw_output?: string | null;
  readonly error_code?: string | null;
  readonly error_message?: string | null;
  readonly created_at: string;
  readonly is_stale?: boolean;
}

export interface MeetingSummary {
  readonly id: string;
  readonly title: string;
  readonly status: MeetingStatus;
  readonly language: string;
  readonly started_at: string | null;
  readonly ended_at: string | null;
  readonly transcript_revision: number;
  readonly content_revision: number;
  readonly interruption_reason?: string | null;
  readonly created_at: string;
}

export interface MeetingDetail extends MeetingSummary {
  readonly audio_source: string;
  readonly metadata?: Record<string, unknown>;
  readonly speakers: Record<string, MeetingSpeaker>;
  readonly latest_minutes?: MeetingMinutesVersion | null;
  readonly updated_at: string;
}

export interface MeetingListResponse {
  readonly items: MeetingSummary[];
  readonly next_cursor: string | null;
}

export interface TranscriptResponse {
  readonly meeting_id: string;
  readonly transcript_revision: number;
  readonly content_revision: number;
  readonly segments: TranscriptSegment[];
}

/** V1 HTTP 通用错误响应 (§13.1) */
export interface ApiErrorEnvelope {
  readonly error: {
    readonly code: string;
    readonly message: string;
    readonly request_id?: string;
    readonly details?: Record<string, unknown>;
  };
}

export class ApiError extends Error {
  readonly code: string;
  readonly requestId?: string;
  readonly details?: Record<string, unknown>;

  constructor(message: string, code: string, requestId?: string, details?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }
}

/** 规范错误码友好说明映射表 (§13.1) */
export const ERROR_CODE_MESSAGES: Record<string, string> = {
  invalid_request: "请求参数不符合规范",
  not_found: "会议或资源不存在",
  conflict: "操作冲突（如会议正在录制中）",
  storage_unavailable: "PostgreSQL 会议存储暂不可用",
  transcription_unavailable: "WhisperLiveKit 语音转录服务不可用",
  mode_conflict: "运行模式冲突，请先结束当前活动模式",
  meeting_not_active: "目标会议未处于活动状态",
  finalization_timeout: "会议转录冲刷超时，已封存当前数据",
  summary_unavailable: "AI 纪要模型服务不可用",
  internal_error: "服务端内部异常",
  service_unavailable: "服务连接未建立或不可达",
  timeout: "请求处理超时",
};

export function getErrorMessageByCode(code: string, fallbackMessage?: string): string {
  return ERROR_CODE_MESSAGES[code] || fallbackMessage || `未知错误 (${code})`;
}

/** WebSocket V1 Envelope 与事件载荷 (§14.2) */
export type MeetingEventType =
  | "meeting_snapshot"
  | "meeting_state_changed"
  | "transcript_partial"
  | "transcript_reconciled"
  | "speaker_updated"
  | "minutes_state_changed"
  | "health_changed"
  | "transcription_gap"
  | "resync_required";

export interface MeetingEventEnvelope<T = unknown> {
  readonly contract_version: "1";
  readonly type: MeetingEventType;
  readonly event_id: string;
  readonly meeting_id: string;
  readonly occurred_at: string;
  readonly payload: T;
}

export interface MeetingSnapshotPayload {
  readonly meeting: MeetingSummary;
  readonly health?: {
    readonly storage?: StorageHealth;
    readonly transcription?: string;
    readonly mic_muted?: boolean;
    readonly recovery_journal_active?: boolean;
  };
  readonly partial?: string | null;
  readonly transcript_revision: number;
  readonly content_revision: number;
}

export interface MeetingStateChangedPayload {
  readonly status: MeetingStatus;
  readonly started_at?: string | null;
  readonly ended_at?: string | null;
  readonly interruption_reason?: string | null;
}

export interface TranscriptPartialPayload {
  readonly text: string;
  readonly speaker_key?: string | null;
  readonly speaker_name?: string | null;
}

export interface TranscriptReconciledPayload {
  readonly transcript_revision: number;
  readonly content_revision: number;
  readonly replace_from_ms: number;
  readonly segments: TranscriptSegment[];
}

export interface SpeakerUpdatedPayload {
  readonly speaker_key: string;
  readonly display_name: string;
  readonly content_revision: number;
}

export interface MinutesStateChangedPayload {
  readonly minutes_id?: string;
  readonly version: number;
  readonly status: MinutesStatus;
  readonly error_code?: string | null;
  readonly error_message?: string | null;
  readonly minutes?: MeetingMinutesVersion | null;
}

export interface HealthChangedPayload {
  readonly storage: StorageHealth;
  readonly transcription: "ok" | "gap" | "disconnected" | string;
  readonly mic_muted: boolean;
  readonly recovery_journal_active?: boolean;
}

export interface TranscriptionGapPayload {
  readonly start_ms: number;
  readonly end_ms: number;
  readonly reason?: string;
}

export interface ResyncRequiredPayload {
  readonly expected_revision?: number;
  readonly reason?: string;
}
