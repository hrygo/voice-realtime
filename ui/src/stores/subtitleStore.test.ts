import { describe, expect, it } from "vitest";

import { reduceSubtitleSnapshot, toSRT } from "./subtitleStore";

describe("subtitle snapshot reducer", () => {
  it("replaces confirmed lines and accepts an empty partial", () => {
    const previous = {
      lines: [{ speaker: 0, text: "旧", start: "00:00:00", end: "00:00:01" }],
      partial: "处理中",
    };

    const next = reduceSubtitleSnapshot(previous, { lines: [], buffer_transcription: "" });

    expect(next).toEqual({ lines: [], partial: "" });
  });
});

describe("toSRT", () => {
  it("accepts dot and comma milliseconds and exports comma format", () => {
    const output = toSRT([
      { speaker: 0, text: "第一句", start: "0:00:03.500", end: "0:00:04,125" },
    ]);

    expect(output).toContain("00:00:03,500 --> 00:00:04,125");
  });
});
