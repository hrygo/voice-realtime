import { describe, expect, it } from "vitest";

import {
  createAssistantSnapshot,
  parseAssistantEvent,
  reduceAssistantEvent,
  selectAgentReplies,
  useAssistantStore,
} from "./assistantStore";

describe("reduceAssistantEvent", () => {
  it("initializes in listening mode and returns to listening when TTS stops", () => {
    const initial = createAssistantSnapshot();
    expect(initial.phase).toBe("listening");
    expect(initial.activity.listening).toBe(true);

    const pipelineStarted = reduceAssistantEvent(initial, {
      type: "system",
      state: "pipeline_started",
    });
    expect(pipelineStarted.phase).toBe("listening");

    const speaking = reduceAssistantEvent(pipelineStarted, {
      type: "tts",
      state: "started",
    });
    expect(speaking.phase).toBe("speaking");

    const finished = reduceAssistantEvent(speaking, {
      type: "tts",
      state: "stopped",
    });
    expect(finished.phase).toBe("listening");
    expect(finished.activity.listening).toBe(true);
  });

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

  it("records every microphone speech start even when final STT immediately follows", () => {
    const started = reduceAssistantEvent(createAssistantSnapshot(), {
      type: "vad",
      state: "user_speaking",
    });
    const finalized = reduceAssistantEvent(started, {
      type: "stt",
      state: "final",
      text: "你好",
    });

    expect(finalized.phase).toBe("thinking");
    expect(finalized.speechSequence).toBe(1);
  });

  it("enters thinking after final STT with valid text, but returns to listening on punctuation-only STT", () => {
    const valid = reduceAssistantEvent(createAssistantSnapshot(), {
      type: "stt",
      state: "final",
      text: "你好",
    });
    expect(valid.phase).toBe("thinking");

    const punctuationOnly = reduceAssistantEvent(valid, {
      type: "stt",
      state: "final",
      text: "。",
    });
    expect(punctuationOnly.phase).toBe("listening");
  });

  it("resets thinking state to listening on interruption", () => {
    const thinking = reduceAssistantEvent(createAssistantSnapshot(), {
      type: "stt",
      state: "final",
      text: "你好",
    });
    expect(thinking.phase).toBe("thinking");

    const interrupted = reduceAssistantEvent(thinking, {
      type: "interruption",
      state: "detected",
    });
    expect(interrupted.phase).toBe("listening");
  });

  it("does not return to thinking when the LLM final marker arrives after playback and stays in listening", () => {
    const speaking = reduceAssistantEvent(createAssistantSnapshot(), {
      type: "tts",
      state: "started",
    });
    const listening = reduceAssistantEvent(speaking, { type: "tts", state: "stopped" });
    expect(listening.phase).toBe("listening");

    const finalized = reduceAssistantEvent(listening, {
      type: "llm",
      state: "final",
      text: "",
      turnId: 0,
    });

    expect(finalized.phase).toBe("listening");
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

  it("preserves repeated user inputs in subsequent conversation turns", () => {
    let snapshot = createAssistantSnapshot();
    // Turn 1: user says "没有", AI responds
    snapshot = reduceAssistantEvent(snapshot, { type: "stt", state: "final", text: "没有" });
    snapshot = reduceAssistantEvent(snapshot, {
      type: "llm",
      state: "final",
      text: "心情不好呀？",
      turnId: 1,
    });
    expect(snapshot.transcript).toHaveLength(2);
    expect(snapshot.transcript[0].text).toBe("没有");

    // Turn 2: user repeats "没有", AI responds
    snapshot = reduceAssistantEvent(snapshot, { type: "stt", state: "interim", text: "没有" });
    snapshot = reduceAssistantEvent(snapshot, { type: "stt", state: "final", text: "没有" });
    expect(snapshot.transcript).toHaveLength(3);
    expect(snapshot.transcript[2].role).toBe("user");
    expect(snapshot.transcript[2].text).toBe("没有");

    // Turn 3: user repeats "没有" again
    snapshot = reduceAssistantEvent(snapshot, {
      type: "llm",
      state: "final",
      text: "不想聊也没关系",
      turnId: 2,
    });
    snapshot = reduceAssistantEvent(snapshot, { type: "stt", state: "final", text: "没有" });
    expect(snapshot.transcript).toHaveLength(5);
    expect(snapshot.transcript[4].role).toBe("user");
    expect(snapshot.transcript[4].text).toBe("没有");
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

describe("useAssistantStore runtime watchdog & disconnect", () => {
  it("resets active thinking state to listening on disconnect", () => {
    useAssistantStore.setState({
      phase: "thinking",
      activity: { listening: false, thinking: true, speaking: false },
      connected: true,
    });

    useAssistantStore.getState().setConnected(false);

    expect(useAssistantStore.getState().phase).toBe("listening");
    expect(useAssistantStore.getState().activity.thinking).toBe(false);
  });
});
