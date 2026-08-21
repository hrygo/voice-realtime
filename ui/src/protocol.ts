import type { MeetingStatus, RuntimeMode } from "./contracts/meetingContract";

export type DuplexMode = "speaker_focus" | "headphone_duplex";

export interface RuntimeStateSnapshot {
  readonly mode?: RuntimeMode;
  readonly active_meeting_id?: string | null;
  readonly meeting_state?: MeetingStatus | null;
  readonly meeting_started_at?: string | null;
  readonly pipeline: string;
  readonly subtitle: string;
  readonly storage?: string;
  readonly mic_muted: boolean;
  readonly runtime_revision?: number;
  readonly persona?: string | null;
  readonly voice?: string;
  readonly duplex_mode?: DuplexMode;
  readonly session_started_at?: string | null;
}

export type ControlCommand =
  | { readonly cmd: "clear_context" }
  | { readonly cmd: "stop_session" }
  | { readonly cmd: "restart" }
  | { readonly cmd: "set_persona"; readonly prompt: string }
  | { readonly cmd: "set_voice"; readonly voice: string }
  | { readonly cmd: "set_duplex_mode"; readonly mode: DuplexMode }
  | { readonly cmd: "set_mic_muted"; readonly muted: boolean }
  | { readonly cmd: "start_meeting"; readonly title?: string; readonly contract_version?: "1" }
  | { readonly cmd: "end_meeting"; readonly meeting_id?: string; readonly contract_version?: "1" }
  | { readonly cmd: "start_assistant"; readonly contract_version?: "1" }
  | { readonly cmd: "stop_active_mode"; readonly contract_version?: "1" };

export interface CommandResponse {
  readonly contract_version?: "1";
  readonly request_id: string;
  readonly cmd: string;
  readonly ok: boolean;
  readonly state: RuntimeStateSnapshot;
  readonly error_code?: string | null;
  readonly message?: string | null;
  readonly error?: {
    readonly code: string;
    readonly message: string;
    readonly request_id?: string;
    readonly details?: Record<string, unknown>;
  } | null;
}

export function isRuntimeState(value: unknown): value is RuntimeStateSnapshot {
  if (!isRecord(value)) return false;
  return typeof value.pipeline === "string"
    && typeof value.subtitle === "string"
    && typeof value.mic_muted === "boolean"
    && (value.persona === undefined || typeof value.persona === "string" || value.persona === null)
    && (value.voice === undefined || typeof value.voice === "string")
    && (value.duplex_mode === undefined || value.duplex_mode === "speaker_focus" || value.duplex_mode === "headphone_duplex")
    && (value.session_started_at === undefined || typeof value.session_started_at === "string" || value.session_started_at === null);
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
