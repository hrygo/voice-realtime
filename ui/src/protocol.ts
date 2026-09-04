import type { MeetingStatus, PCMOwner, RuntimeMode } from "./contracts/meetingContract";

export type DuplexMode = "speaker_focus" | "headphone_duplex";

export interface AudioLevelsSnapshot {
  readonly microphone: number;
  readonly physical_output: number;
  readonly mixed: number;
  readonly updated_at_ns: number;
}

export type ServiceProbeStatus = "ok" | "unreachable" | "timeout" | "error";

export interface ServiceInfo {
  readonly name: string;
  readonly status: ServiceProbeStatus;
  readonly url: string;
  readonly target_model?: string | null;
  readonly model_present?: boolean | null;
  readonly workload?: string | null;
  readonly ws_state?: string | null;
  readonly reconnect_count?: number | null;
  readonly last_event_age_ms?: number | null;
  readonly dropped_chunks?: number | null;
  readonly gap_count?: number | null;
}

export interface ServicesResponse {
  readonly services: ServiceInfo[];
  readonly diagnostics?: unknown;
  readonly network_scope?: "local" | "network";
}

export interface RuntimeStateSnapshot {
  readonly mode: RuntimeMode;
  readonly pcm_owner: PCMOwner;
  readonly active_meeting_id?: string | null;
  readonly meeting_state?: MeetingStatus | null;
  readonly meeting_started_at?: string | null;
  readonly pipeline: string;
  readonly subtitle: string;
  readonly storage?: string;
  readonly mic_muted: boolean;
  readonly runtime_revision: number;
  readonly audio_levels?: AudioLevelsSnapshot;
  readonly persona?: string | null;
  readonly voice?: string;
  readonly duplex_mode?: DuplexMode;
  readonly session_started_at?: string | null;
  readonly degraded_reason?: string | null;
  readonly capabilities?: {
    readonly inner_os_enabled: boolean;
    readonly inner_os_analysis_enabled: boolean;
    readonly inner_os_channel: "loopback_only";
    readonly diarization_overlay_enabled?: boolean;
  };
}

export type ControlCommand =
  | { readonly cmd: "clear_context" }
  | { readonly cmd: "clear_subtitles" }
  | { readonly cmd: "stop_session" }
  | { readonly cmd: "restart" }
  | { readonly cmd: "set_persona"; readonly prompt: string }
  | { readonly cmd: "set_voice"; readonly voice: string }
  | { readonly cmd: "set_duplex_mode"; readonly mode: DuplexMode }
  | { readonly cmd: "set_mic_muted"; readonly muted: boolean }
  | { readonly cmd: "start_meeting"; readonly title?: string; readonly max_speakers?: number; readonly contract_version?: "1" }
  | { readonly cmd: "end_meeting"; readonly meeting_id?: string; readonly contract_version?: "1" }
  | { readonly cmd: "start_assistant"; readonly contract_version?: "1" }
  | { readonly cmd: "start_subtitles"; readonly contract_version?: "1" }
  | { readonly cmd: "stop_active_mode"; readonly contract_version?: "1" }
  | { readonly cmd: "send_text"; readonly text: string };

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

export function isServicesResponse(value: unknown): value is ServicesResponse {
  return isRecord(value)
    && Array.isArray(value.services)
    && value.services.every(isServiceInfo)
    && (value.network_scope === undefined
      || value.network_scope === "local"
      || value.network_scope === "network");
}

function isServiceInfo(value: unknown): value is ServiceInfo {
  return isRecord(value)
    && typeof value.name === "string"
    && isServiceProbeStatus(value.status)
    && typeof value.url === "string"
    && isOptionalString(value.target_model)
    && isOptionalBoolean(value.model_present)
    && isOptionalString(value.workload)
    && isOptionalString(value.ws_state)
    && isOptionalNonNegativeInteger(value.reconnect_count)
    && isOptionalNonNegativeInteger(value.last_event_age_ms)
    && isOptionalNonNegativeInteger(value.dropped_chunks)
    && isOptionalNonNegativeInteger(value.gap_count);
}

function isServiceProbeStatus(value: unknown): value is ServiceProbeStatus {
  return value === "ok"
    || value === "unreachable"
    || value === "timeout"
    || value === "error";
}

function isOptionalString(value: unknown): value is string | null | undefined {
  return value === undefined || value === null || typeof value === "string";
}

function isOptionalBoolean(value: unknown): value is boolean | null | undefined {
  return value === undefined || value === null || typeof value === "boolean";
}

function isOptionalNonNegativeInteger(value: unknown): value is number | null | undefined {
  return value === undefined
    || value === null
    || (typeof value === "number" && Number.isInteger(value) && value >= 0);
}

export function isRuntimeState(value: unknown): value is RuntimeStateSnapshot {
  if (!isRecord(value)) return false;
  return isRuntimeMode(value.mode)
    && isPCMOwner(value.pcm_owner)
    && typeof value.runtime_revision === "number"
    && Number.isInteger(value.runtime_revision)
    && value.runtime_revision >= 0
    && typeof value.pipeline === "string"
    && typeof value.subtitle === "string"
    && typeof value.mic_muted === "boolean"
    && (value.audio_levels === undefined || isAudioLevels(value.audio_levels))
    && (value.active_meeting_id === undefined || typeof value.active_meeting_id === "string" || value.active_meeting_id === null)
    && (value.meeting_state === undefined || isMeetingStatus(value.meeting_state) || value.meeting_state === null)
    && (value.meeting_started_at === undefined || typeof value.meeting_started_at === "string" || value.meeting_started_at === null)
    && (value.storage === undefined || typeof value.storage === "string")
    && (value.persona === undefined || typeof value.persona === "string" || value.persona === null)
    && (value.voice === undefined || typeof value.voice === "string")
    && (value.duplex_mode === undefined || value.duplex_mode === "speaker_focus" || value.duplex_mode === "headphone_duplex")
    && (value.session_started_at === undefined || typeof value.session_started_at === "string" || value.session_started_at === null)
    && (value.degraded_reason === undefined || typeof value.degraded_reason === "string" || value.degraded_reason === null)
    && (value.capabilities === undefined || isRuntimeCapabilities(value.capabilities));
}

function isAudioLevels(value: unknown): value is AudioLevelsSnapshot {
  return isRecord(value)
    && isNormalizedLevel(value.microphone)
    && isNormalizedLevel(value.physical_output)
    && isNormalizedLevel(value.mixed)
    && typeof value.updated_at_ns === "number"
    && Number.isInteger(value.updated_at_ns)
    && value.updated_at_ns >= 0;
}

function isNormalizedLevel(value: unknown): value is number {
  return typeof value === "number"
    && Number.isFinite(value)
    && value >= 0
    && value <= 1;
}

function isRuntimeCapabilities(value: unknown): value is RuntimeStateSnapshot["capabilities"] {
  return isRecord(value)
    && typeof value.inner_os_enabled === "boolean"
    && typeof value.inner_os_analysis_enabled === "boolean"
    && value.inner_os_channel === "loopback_only"
    && (value.diarization_overlay_enabled === undefined
      || typeof value.diarization_overlay_enabled === "boolean");
}

function isRuntimeMode(value: unknown): value is RuntimeMode {
  return value === "assistant" || value === "subtitles" || value === "meeting" || value === "idle";
}

function isPCMOwner(value: unknown): value is PCMOwner {
  return value === "assistant" || value === "subtitles" || value === "meeting" || value === "none";
}

function isMeetingStatus(value: unknown): value is MeetingStatus {
  return value === "recording"
    || value === "finalizing"
    || value === "completed"
    || value === "interrupted"
    || value === "storage_error";
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
