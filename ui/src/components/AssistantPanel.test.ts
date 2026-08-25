import { describe, expect, it } from "vitest";

import {
  DUPLEX_MODE_PRESENTATION,
  canRequestDuplexModeChange,
  getTelemetryBadge,
  TELEMETRY_HELP_STEPS,
  getAssistantPhaseTransitionDelay,
  getDuplexModeFeedback,
  getDuplexToggleMode,
} from "./AssistantPanel";

describe("assistant phase presentation", () => {
  it("keeps the listening animation visible before advancing to thinking", () => {
    expect(getAssistantPhaseTransitionDelay("listening", "thinking", 1_000, 1_100)).toBe(140);
  });

  it("does not delay entering listening when speech starts", () => {
    expect(getAssistantPhaseTransitionDelay("idle", "listening", 1_000, 1_100)).toBe(0);
  });
});

describe("duplex mode presentation", () => {
  it("names speaker mode by its output device and interaction consequence", () => {
    expect(DUPLEX_MODE_PRESENTATION.speaker_focus.interruptionEnabled).toBe(false);
    expect(DUPLEX_MODE_PRESENTATION.speaker_focus.label).toBe("扬声器");
    expect(DUPLEX_MODE_PRESENTATION.speaker_focus.summary).toContain("不接收插话");
    expect(DUPLEX_MODE_PRESENTATION.speaker_focus.detail).toContain("扬声器");
  });

  it("names headphone mode by its output device and interaction consequence", () => {
    expect(DUPLEX_MODE_PRESENTATION.headphone_duplex.interruptionEnabled).toBe(true);
    expect(DUPLEX_MODE_PRESENTATION.headphone_duplex.label).toBe("耳机");
    expect(DUPLEX_MODE_PRESENTATION.headphone_duplex.summary).toContain("可随时插话");
    expect(DUPLEX_MODE_PRESENTATION.headphone_duplex.detail).toContain("需佩戴耳机");
  });
});

describe("duplex mode feedback", () => {
  it("explains that the system is applying the requested mode while awaiting acknowledgement", () => {
    const feedback = getDuplexModeFeedback("speaker_focus", "headphone_duplex", true);

    expect(feedback.tone).toBe("switching");
    expect(feedback.title).toContain("耳机");
    expect(feedback.detail).toContain("正在应用");
  });

  it("keeps the active mode visible when a switch fails", () => {
    const feedback = getDuplexModeFeedback("speaker_focus", null, true, "切换失败");

    expect(feedback.tone).toBe("error");
    expect(feedback.detail).toContain("扬声器");
  });

  it("does not submit a duplicate request for the active or pending mode", () => {
    expect(canRequestDuplexModeChange("speaker_focus", "speaker_focus", null)).toBe(false);
    expect(canRequestDuplexModeChange("speaker_focus", "headphone_duplex", "headphone_duplex")).toBe(false);
    expect(canRequestDuplexModeChange("speaker_focus", "headphone_duplex", null)).toBe(true);
  });
});

describe("duplex toggle position", () => {
  it("shows the committed mode when no switch is pending", () => {
    expect(getDuplexToggleMode("speaker_focus", null)).toBe("speaker_focus");
  });

  it("moves the visual thumb to the requested mode before acknowledgement", () => {
    expect(getDuplexToggleMode("speaker_focus", "headphone_duplex")).toBe("headphone_duplex");
  });

  it("returns to the committed mode when a pending switch is cleared", () => {
    expect(getDuplexToggleMode("headphone_duplex", null)).toBe("headphone_duplex");
  });
});

describe("telemetry badge", () => {
  it("shows the grade and total for complete metrics", () => {
    expect(getTelemetryBadge({
      sttMs: 150,
      llmTtftMs: 700,
      ttsTtfbMs: 250,
      e2eMs: 1100,
    })).toEqual({ className: "fast", label: "极速", value: 1100 });
  });

  it("shows data insufficiency for partial metrics", () => {
    expect(getTelemetryBadge({
      sttMs: null,
      llmTtftMs: 700,
      ttsTtfbMs: 250,
      e2eMs: null,
    })).toEqual({ className: "idle", label: "数据不足", value: null });
  });

  it("shows standby before the first metrics event", () => {
    expect(getTelemetryBadge(null)).toEqual({ className: "idle", label: "待命中", value: null });
  });
});

describe("telemetry explanation", () => {
  it("documents the anchor and all three measured stages", () => {
    expect(TELEMETRY_HELP_STEPS.map((step) => step.title)).toEqual([
      "STT 识别",
      "LLM 首字",
      "TTS 首包",
    ]);
    expect(TELEMETRY_HELP_STEPS[0].formula).toContain("STT final");
    expect(TELEMETRY_HELP_STEPS[1].formula).toContain("max");
    expect(TELEMETRY_HELP_STEPS[2].formula).toContain("TTS 首帧");
  });

  it("makes the frame-level meaning explicit for users", () => {
    const explanation = TELEMETRY_HELP_STEPS.map((step) => `${step.event} ${step.description}`).join(" ");
    expect(explanation).toContain("UserStoppedSpeakingFrame");
    expect(explanation).toContain("TranscriptionFrame");
    expect(explanation).toContain("LLMTextFrame");
    expect(explanation).toContain("TTSAudioRawFrame");
  });
});
