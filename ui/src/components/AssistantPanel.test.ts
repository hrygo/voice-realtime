import { describe, expect, it } from "vitest";

import { DUPLEX_MODE_PRESENTATION } from "./AssistantPanel";

describe("duplex mode presentation", () => {
  it("makes speaker mode explicitly non-interruptible during playback", () => {
    expect(DUPLEX_MODE_PRESENTATION.speaker_focus.interruptionEnabled).toBe(false);
    expect(DUPLEX_MODE_PRESENTATION.speaker_focus.summary).toContain("不可打断");
  });

  it("makes headphone mode explicitly interruptible during playback", () => {
    expect(DUPLEX_MODE_PRESENTATION.headphone_duplex.interruptionEnabled).toBe(true);
    expect(DUPLEX_MODE_PRESENTATION.headphone_duplex.summary).toContain("可以插话");
  });
});
