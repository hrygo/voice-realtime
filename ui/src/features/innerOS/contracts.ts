/**
 * Sona 会议助手『内心 OS』前端契约与运行时校验定义
 * 依据 contracts/meeting-assistant/v1/schemas/
 */

export type InnerOSIntent = "fact" | "analysis" | "draft" | "mixed";

export type InnerOSUncertainty = "low" | "medium" | "high";

export interface InnerOSEphemeralContext {
  readonly goal?: string;
  readonly agenda?: string;
  readonly background?: string;
}

export interface InnerOSEvidenceItem {
  readonly segment_id: string;
  readonly start_ms: number;
  readonly end_ms: number;
  readonly speaker_key: string;
  readonly speaker_name: string;
  readonly text: string;
  readonly content_hash: string;
}

export interface InnerOSFactItem {
  readonly text: string;
  readonly evidence_segment_ids: readonly string[];
}

export interface InnerOSJudgementItem {
  readonly text: string;
  readonly basis_segment_ids: readonly string[];
  readonly uncertainty: InnerOSUncertainty;
  readonly uncertainty_reason: string;
}

export interface InnerOSDraft {
  readonly text: string;
}

export interface InnerOSLimitation {
  readonly code: string;
  readonly message: string;
}

export interface InnerOSAnswer {
  readonly intent: InnerOSIntent;
  readonly evidence: readonly InnerOSEvidenceItem[];
  readonly facts: readonly InnerOSFactItem[];
  readonly judgements: readonly InnerOSJudgementItem[];
  readonly draft: InnerOSDraft | null;
  readonly limitations: readonly InnerOSLimitation[];
}

export interface InnerOSExchange {
  readonly id: string;
  readonly meeting_id: string;
  readonly question: string;
  readonly intent: InnerOSIntent;
  readonly answer: InnerOSAnswer;
  readonly source_transcript_revision: number;
  readonly source_content_revision: number;
  readonly used_ephemeral_context: boolean;
  readonly context_advanced?: boolean;
  readonly evidence_invalidated?: boolean;
  readonly model: string;
  readonly reasoning: "off" | "on";
  readonly prompt_version?: string | null;
  readonly created_at: string;
}

export type QuickPromptCategory = "fact" | "analysis" | "draft" | "custom";

export interface QuickPromptItem {
  readonly id: string;
  readonly category: QuickPromptCategory;
  readonly label: string;
  readonly intent: InnerOSIntent;
  readonly question: string;
  readonly isCustom?: boolean;
}

export type DraftTone = "professional" | "concise" | "constructive" | "inquisitive";

export interface InnerOSSessionItem {
  readonly queryId: string;
  readonly meetingId: string;
  readonly question: string;
  readonly intent: InnerOSIntent;
  readonly answer: InnerOSAnswer;
  readonly createdAt: string;
  saved: boolean;
  isExpanded?: boolean;
}

export interface InnerOSExchangeListResponse {
  readonly items: readonly InnerOSExchange[];
  readonly next_cursor: string | null;
}

export type InnerOSEventType =
  | "inner_os_query_accepted"
  | "inner_os_answer_started"
  | "inner_os_answer_completed"
  | "inner_os_answer_failed"
  | "inner_os_answer_cancelled";

export interface InnerOSEventEnvelope<T = unknown> {
  readonly contract_version: "1";
  readonly type: InnerOSEventType;
  readonly event_id: string;
  readonly meeting_id: string;
  readonly query_id: string;
  readonly request_id?: string | null;
  readonly occurred_at: string;
  readonly payload: T;
}

export interface InnerOSQueryAcceptedPayload {
  readonly status: "accepted";
}

export interface InnerOSAnswerStartedPayload {
  readonly status: "started";
  readonly intent?: InnerOSIntent;
  readonly transcript_revision?: number;
  readonly content_revision?: number;
}

export interface InnerOSAnswerCompletedPayload extends InnerOSAnswer {
  readonly transcript_revision?: number;
  readonly content_revision?: number;
  readonly context_advanced?: boolean;
  readonly saved?: boolean;
}

export interface InnerOSAnswerFailedPayload {
  readonly error: {
    readonly code: string;
    readonly message: string;
    readonly request_id?: string;
    readonly details?: Record<string, unknown>;
  };
}

export interface InnerOSAnswerCancelledPayload {
  readonly reason: "user_cancelled" | "connection_closed" | "meeting_finalizing" | string;
}

export interface InnerOSQueryCommand {
  readonly contract_version: "1";
  readonly request_id: string;
  readonly cmd: "query";
  /** Optional for older clients; when present it is reused as the canonical query/exchange ID. */
  readonly query_id?: string;
  readonly meeting_id: string;
  readonly question: string;
  readonly intent: InnerOSIntent;
  readonly context_version: number;
  readonly ephemeral_context?: InnerOSEphemeralContext | null;
  readonly focus_segment_ids?: readonly string[];
}

export interface InnerOSCancelCommand {
  readonly contract_version: "1";
  readonly request_id: string;
  readonly cmd: "cancel";
  readonly query_id: string;
}

/** 运行时校验函数 */
export function isInnerOSAnswer(val: unknown): val is InnerOSAnswer {
  if (!val || typeof val !== "object") return false;
  const o = val as Record<string, unknown>;
  if (!["fact", "analysis", "draft", "mixed"].includes(o.intent as string)) return false;
  if (!Array.isArray(o.evidence) || !Array.isArray(o.facts) || !Array.isArray(o.judgements)) return false;
  if (!Array.isArray(o.limitations)) return false;
  return true;
}

export function isInnerOSEventEnvelope(val: unknown): val is InnerOSEventEnvelope {
  if (!val || typeof val !== "object") return false;
  const o = val as Record<string, unknown>;
  if (o.contract_version !== "1") return false;
  if (typeof o.type !== "string" || !o.type.startsWith("inner_os_")) return false;
  if (typeof o.meeting_id !== "string" || typeof o.query_id !== "string") return false;
  return true;
}
