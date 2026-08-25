import { describe, expect, it } from "vitest";

import { getNextDisplayedPhase } from "./AssistantPhaseDisplay";

describe("getNextDisplayedPhase", () => {
  it("inserts listening when speech start and final STT are observed in one render", () => {
    expect(getNextDisplayedPhase({
      displayedPhase: "idle",
      pipelinePhase: "thinking",
      observedSpeechSequence: 0,
      speechSequence: 1,
      phaseStartedAt: 1_000,
      now: 1_100,
    })).toEqual({
      displayedPhase: "listening",
      observedSpeechSequence: 1,
      delayMs: 240,
    });
  });

  it("preserves the remaining listening window before thinking", () => {
    expect(getNextDisplayedPhase({
      displayedPhase: "listening",
      pipelinePhase: "thinking",
      observedSpeechSequence: 1,
      speechSequence: 1,
      phaseStartedAt: 1_000,
      now: 1_100,
    })).toEqual({
      displayedPhase: "thinking",
      observedSpeechSequence: 1,
      delayMs: 140,
    });
  });
});
