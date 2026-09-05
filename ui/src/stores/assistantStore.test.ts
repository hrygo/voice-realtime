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

  it("retains a pipeline failure and its reason through late LLM and TTS closing events", () => {
    let failed = reduceAssistantEvent(createAssistantSnapshot(), {
      type: "llm",
      state: "streaming",
      text: "已经生成了一半",
      turnId: 0,
    });
    failed = reduceAssistantEvent(failed, {
      type: "system",
      state: "pipeline_error",
      message: "LM Studio 连接失败",
    });

    expect(failed.phase).toBe("degraded");
    expect(failed.errorMessage).toBe("LM Studio 连接失败");

    const afterLlmFinal = reduceAssistantEvent(failed, {
      type: "llm",
      state: "final",
      text: "已经生成了一半，随后连接中断",
      turnId: 0,
    });
    expect(afterLlmFinal.phase).toBe("degraded");
    expect(afterLlmFinal.errorMessage).toBe("LM Studio 连接失败");

    const afterTtsStopped = reduceAssistantEvent(afterLlmFinal, {
      type: "tts",
      state: "stopped",
    });
    expect(afterTtsStopped.phase).toBe("degraded");
    expect(afterTtsStopped.errorMessage).toBe("LM Studio 连接失败");
  });

  it("clears a retained failure only after new input or an explicitly started pipeline", () => {
    const failed = reduceAssistantEvent(createAssistantSnapshot(), {
      type: "system",
      state: "pipeline_error",
      message: "语音服务暂不可用",
    });

    const afterNewInput = reduceAssistantEvent(failed, {
      type: "stt",
      state: "final",
      text: "请再试一次",
    });
    expect(afterNewInput.errorMessage).toBeNull();
    expect(afterNewInput.phase).toBe("thinking");

    const afterRestart = reduceAssistantEvent(failed, {
      type: "system",
      state: "pipeline_started",
    });
    expect(afterRestart.errorMessage).toBeNull();
    expect(afterRestart.phase).toBe("listening");

    const afterNormalReply = reduceAssistantEvent(afterNewInput, {
      type: "llm",
      state: "streaming",
      text: "我已经恢复，可以继续回答了。",
      turnId: 1,
    });
    expect(afterNormalReply.errorMessage).toBeNull();
    expect(afterNormalReply.phase).toBe("thinking");
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

  it("clears a degraded notice for an explicit retry without inserting a transcript bubble", () => {
    useAssistantStore.setState({
      phase: "degraded",
      activity: { listening: false, thinking: false, speaking: false },
      errorMessage: "语音服务暂不可用",
      transcript: [],
    });

    useAssistantStore.getState().clearError();

    expect(useAssistantStore.getState().errorMessage).toBeNull();
    expect(useAssistantStore.getState().phase).toBe("listening");
    expect(useAssistantStore.getState().transcript).toEqual([]);
  });
});
