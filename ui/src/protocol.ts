export type DuplexMode = "speaker_focus" | "headphone_duplex";

export interface RuntimeStateSnapshot {
  readonly pipeline: string;
  readonly subtitle: string;
  readonly mic_muted: boolean;
  readonly persona: string | null;
  readonly voice: string;
  readonly duplex_mode: DuplexMode;
  readonly session_started_at: string | null;
}

export type ControlCommand =
  | { readonly cmd: "clear_context" }
  | { readonly cmd: "stop_session" }
  | { readonly cmd: "restart" }
  | { readonly cmd: "set_persona"; readonly prompt: string }
  | { readonly cmd: "set_voice"; readonly voice: string }
  | { readonly cmd: "set_duplex_mode"; readonly mode: DuplexMode }
  | { readonly cmd: "set_mic_muted"; readonly muted: boolean };

export interface CommandResponse {
  readonly request_id: string;
  readonly cmd: string;
  readonly ok: boolean;
  readonly state: RuntimeStateSnapshot;
  readonly error_code: string | null;
  readonly message: string | null;
}

export function isRuntimeState(value: unknown): value is RuntimeStateSnapshot {
  if (!isRecord(value)) return false;
  return typeof value.pipeline === "string"
    && typeof value.subtitle === "string"
    && typeof value.mic_muted === "boolean"
    && (typeof value.persona === "string" || value.persona === null)
    && typeof value.voice === "string"
    && (value.duplex_mode === "speaker_focus" || value.duplex_mode === "headphone_duplex")
    && (typeof value.session_started_at === "string" || value.session_started_at === null);
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
