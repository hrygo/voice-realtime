import { describe, expect, it } from "vitest";

import { reduceSubtitleSnapshot, toSRT } from "./subtitleStore";

describe("subtitle snapshot reducer", () => {
  it("replaces confirmed lines and accepts an empty partial", () => {
    const previous = {
      lines: [{ speaker: 0, text: "旧", start: "00:00:00", end: "00:00:01" }],
      partial: "处理中",
    };

    const next = reduceSubtitleSnapshot(previous, { lines: [], buffer_transcription: "" });

    expect(next.lines).toEqual([]);
    expect(next.partial).toBe("");
  });

  it("filters out old raw lines when clearedOffset is set", () => {
    const previous = {
      rawLines: [
        { speaker: 0, text: "第一句", start: "00:00:00", end: "00:00:01" },
        { speaker: 1, text: "第二句", start: "00:00:01", end: "00:00:02" },
      ],
      lines: [],
      partial: "",
      clearedOffset: 2,
    };

    const snapshotWithNewLines = {
      lines: [
        { speaker: 0, text: "第一句", start: "00:00:00", end: "00:00:01" },
        { speaker: 1, text: "第二句", start: "00:00:01", end: "00:00:02" },
        { speaker: 0, text: "第三句 (新)", start: "00:00:02", end: "00:00:03" },
      ],
      buffer_transcription: "正在说话",
    };

    const next = reduceSubtitleSnapshot(previous, snapshotWithNewLines);

    expect(next.lines).toHaveLength(1);
    expect(next.lines[0]?.text).toBe("第三句 (新)");
    expect(next.partial).toBe("正在说话");
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
