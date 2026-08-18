import { create } from "zustand";

/** 助手管道当前可见的活动相位。 */
export type AssistantPhase = "idle" | "listening" | "thinking" | "speaking";

/** 对话气泡：未落定的助手气泡由组件展示为流式状态。 */
export interface AssistantBubble {
  readonly role: "user" | "assistant";
  readonly text: string;
  readonly turnId?: number;
  readonly final: boolean;
}

type AssistantActivity = {
  readonly listening: boolean;
  readonly thinking: boolean;
  readonly speaking: boolean;
};

export interface AssistantSnapshot {
  readonly phase: AssistantPhase;
  readonly activity: AssistantActivity;
  readonly transcript: readonly AssistantBubble[];
}

export type AssistantEvent =
  | { readonly type: "vad"; readonly state: "user_speaking" | "user_silence" }
  | { readonly type: "stt"; readonly state: "interim" | "final"; readonly text: string }
  | { readonly type: "llm"; readonly state: "streaming" | "final"; readonly text: string; readonly turnId: number }
  | { readonly type: "tts"; readonly state: "synthesizing" | "started" | "stopped" }
  | { readonly type: "interruption"; readonly state: "detected" }
  | { readonly type: "system"; readonly state: "pipeline_started" | "pipeline_stopped" | "user_stopped" };

interface AssistantStore extends AssistantSnapshot {
  readonly connected: boolean;
  applyEvent: (event: AssistantEvent) => void;
  setConnected: (connected: boolean) => void;
  clearTranscript: () => void;
}

const MAX_TURNS = 100;
const IDLE_ACTIVITY: AssistantActivity = { listening: false, thinking: false, speaking: false };
const INITIAL_SNAPSHOT: AssistantSnapshot = {
  phase: "idle",
  activity: IDLE_ACTIVITY,
  transcript: [],
};

/** WebSocket 边界解析：协议外的帧由调用方静默忽略。 */
export function parseAssistantEvent(value: unknown): AssistantEvent | null {
  if (!isRecord(value) || typeof value.type !== "string" || typeof value.state !== "string") {
    return null;
  }

  switch (value.type) {
    case "vad":
      return value.state === "user_speaking" || value.state === "user_silence"
        ? { type: "vad", state: value.state }
        : null;
    case "stt":
      return (value.state === "interim" || value.state === "final") && typeof value.text === "string"
        ? { type: "stt", state: value.state, text: value.text }
        : null;
    case "llm":
      return (value.state === "streaming" || value.state === "final")
        && typeof value.text === "string"
        && typeof value.turn_id === "number"
        ? { type: "llm", state: value.state, text: value.text, turnId: value.turn_id }
        : null;
    case "tts":
      return value.state === "synthesizing" || value.state === "started" || value.state === "stopped"
        ? { type: "tts", state: value.state }
        : null;
    case "interruption":
      return value.state === "detected" ? { type: "interruption", state: "detected" } : null;
    case "system":
      return value.state === "pipeline_started"
        || value.state === "pipeline_stopped"
        || value.state === "user_stopped"
        ? { type: "system", state: value.state }
        : null;
    default:
      return null;
  }
}

/** 事件归约器保持纯粹，便于在无 React 环境下复用和测试。 */
export function reduceAssistantEvent(snapshot: AssistantSnapshot, event: AssistantEvent): AssistantSnapshot {
  switch (event.type) {
    case "vad":
      return event.state === "user_speaking"
        ? withActivity(snapshot, { ...snapshot.activity, listening: true })
        : snapshot;
    case "stt":
      return withActivity(
        { ...snapshot, transcript: updateUserTranscript(snapshot.transcript, event) },
        { ...snapshot.activity, listening: true },
      );
    case "llm":
      return withActivity(
        { ...snapshot, transcript: updateAssistantTranscript(snapshot.transcript, event) },
        { ...snapshot.activity, listening: false, thinking: event.state === "streaming" },
      );
    case "tts":
      if (event.state === "stopped") {
        return withActivity(snapshot, IDLE_ACTIVITY);
      }
      return withActivity(snapshot, { listening: false, thinking: false, speaking: true });
    case "interruption":
      return withActivity(
        { ...snapshot, transcript: snapshot.transcript.filter((bubble) => bubble.role === "user" || bubble.final) },
        { listening: true, thinking: false, speaking: false },
      );
    case "system":
      return event.state === "pipeline_started" ? snapshot : INITIAL_SNAPSHOT;
  }
}

export const useAssistantStore = create<AssistantStore>((set) => ({
  ...INITIAL_SNAPSHOT,
  connected: false,
  applyEvent: (event) => set((state) => reduceAssistantEvent(state, event)),
  setConnected: (connected) => set({ connected }),
  clearTranscript: () => set((state) => ({ ...state, transcript: [] })),
}));

/** 供组件按字段订阅，避免为无关状态重复渲染。 */
export const selectAssistantPhase = (state: AssistantStore): AssistantPhase => state.phase;
export const selectAssistantTranscript = (state: AssistantStore): readonly AssistantBubble[] => state.transcript;
export const selectAssistantConnected = (state: AssistantStore): boolean => state.connected;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function withActivity(snapshot: AssistantSnapshot, activity: AssistantActivity): AssistantSnapshot {
  return { ...snapshot, activity, phase: phaseFor(activity) };
}

function phaseFor(activity: AssistantActivity): AssistantPhase {
  if (activity.thinking) return "thinking";
  if (activity.speaking) return "speaking";
  if (activity.listening) return "listening";
  return "idle";
}

function updateUserTranscript(
  transcript: readonly AssistantBubble[],
  event: Extract<AssistantEvent, { readonly type: "stt" }>,
): readonly AssistantBubble[] {
  if (!event.text.trim()) return transcript;

  const draftIndex = lastIndexOf(transcript, (bubble) => bubble.role === "user" && !bubble.final);
  if (event.state === "interim") {
    return draftIndex >= 0
      ? replaceBubble(transcript, draftIndex, { ...transcript[draftIndex], text: event.text })
      : limitTranscript([...transcript, { role: "user", text: event.text, final: false }]);
  }

  const duplicate = transcript.some((bubble) => bubble.role === "user" && bubble.final && bubble.text === event.text);
  if (duplicate) return draftIndex >= 0 ? transcript.filter((_, index) => index !== draftIndex) : transcript;
  return draftIndex >= 0
    ? replaceBubble(transcript, draftIndex, { ...transcript[draftIndex], text: event.text, final: true })
    : limitTranscript([...transcript, { role: "user", text: event.text, final: true }]);
}

function updateAssistantTranscript(
  transcript: readonly AssistantBubble[],
  event: Extract<AssistantEvent, { readonly type: "llm" }>,
): readonly AssistantBubble[] {
  const draftIndex = lastIndexOf(
    transcript,
    (bubble) => bubble.role === "assistant" && bubble.turnId === event.turnId && !bubble.final,
  );
  if (event.state === "streaming") {
    return draftIndex >= 0
      ? replaceBubble(transcript, draftIndex, { ...transcript[draftIndex], text: `${transcript[draftIndex].text}${event.text}` })
      : limitTranscript([...transcript, { role: "assistant", text: event.text, turnId: event.turnId, final: false }]);
  }
  if (draftIndex >= 0) {
    const text = event.text || transcript[draftIndex].text;
    return replaceBubble(transcript, draftIndex, { ...transcript[draftIndex], text, final: true });
  }
  return event.text
    ? limitTranscript([...transcript, { role: "assistant", text: event.text, turnId: event.turnId, final: true }])
    : transcript;
}

function lastIndexOf(
  bubbles: readonly AssistantBubble[],
  predicate: (bubble: AssistantBubble) => boolean,
): number {
  for (let index = bubbles.length - 1; index >= 0; index -= 1) {
    const bubble = bubbles[index];
    if (bubble && predicate(bubble)) return index;
  }
  return -1;
}

function replaceBubble(
  bubbles: readonly AssistantBubble[],
  index: number,
  bubble: AssistantBubble,
): readonly AssistantBubble[] {
  return bubbles.map((item, itemIndex) => (itemIndex === index ? bubble : item));
}

function limitTranscript(bubbles: readonly AssistantBubble[]): readonly AssistantBubble[] {
  return bubbles.length <= MAX_TURNS ? bubbles : bubbles.slice(-MAX_TURNS);
}
