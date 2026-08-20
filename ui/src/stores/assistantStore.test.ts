import { describe, expect, it } from "vitest";

import {
  createAssistantSnapshot,
  parseAssistantEvent,
  reduceAssistantEvent,
  selectAgentReplies,
} from "./assistantStore";

describe("reduceAssistantEvent", () => {
  it("enters thinking after user silence", () => {
    const listening = reduceAssistantEvent(createAssistantSnapshot(), {
      type: "vad",
      state: "user_speaking",
    });

    const next = reduceAssistantEvent(listening, { type: "vad", state: "user_silence" });

    expect(next.phase).toBe("thinking");
  });

  it("always enters listening on user speech and keeps speaking during TTS audio", () => {
    const speaking = reduceAssistantEvent(createAssistantSnapshot(), {
      type: "tts",
      state: "started",
    });
    expect(reduceAssistantEvent(speaking, { type: "vad", state: "user_speaking" }).phase)
      .toBe("listening");
    expect(reduceAssistantEvent(speaking, { type: "tts", state: "synthesizing" }).phase)
      .toBe("speaking");
  });

  it("enters thinking after final STT", () => {
    const next = reduceAssistantEvent(createAssistantSnapshot(), {
      type: "stt",
      state: "final",
      text: "你好",
    });

    expect(next.phase).toBe("thinking");
  });

  it("does not return to thinking when the LLM final marker arrives after playback", () => {
    const speaking = reduceAssistantEvent(createAssistantSnapshot(), {
      type: "tts",
      state: "started",
    });
    const idle = reduceAssistantEvent(speaking, { type: "tts", state: "stopped" });

    const finalized = reduceAssistantEvent(idle, {
      type: "llm",
      state: "final",
      text: "",
      turnId: 0,
    });

    expect(finalized.phase).toBe("idle");
  });

  it("maps stopped and error system states deterministically", () => {
    const speaking = reduceAssistantEvent(createAssistantSnapshot(), {
      type: "tts",
      state: "started",
    });

    expect(
      reduceAssistantEvent(speaking, { type: "system", state: "pipeline_stopped" }).phase,
    ).toBe("stopped");
    expect(
      reduceAssistantEvent(speaking, { type: "system", state: "pipeline_error" }).phase,
    ).toBe("degraded");
  });

  it("keeps unavailable latency metrics as null instead of fake zeroes", () => {
    expect(parseAssistantEvent({ type: "metrics", turn_id: 7, stt_ms: null }))
      .toMatchObject({ turnId: 7, sttMs: null, llmTtftMs: null });
  });

  it("exposes streaming and final agent replies for the subtitle panel", () => {
    const transcript = [
      { role: "user" as const, text: "你好", final: true },
      { role: "assistant" as const, text: "你好呀", final: false, turnId: 3, timestamp: "01:02:03" },
      { role: "assistant" as const, text: "今天想聊什么？", final: true, turnId: 4 },
    ];

    expect(selectAgentReplies(transcript, "")).toEqual([transcript[1], transcript[2]]);
    expect(selectAgentReplies(transcript, "今天")).toEqual([transcript[2]]);
  });
});
