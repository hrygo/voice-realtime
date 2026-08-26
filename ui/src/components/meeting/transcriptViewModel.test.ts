import { describe, expect, it } from "vitest";
import type { TranscriptSegment } from "../../contracts/meetingContract";
import {
  deriveReadingBlocks,
  getSegmentsForBlock,
} from "./transcriptViewModel";

describe("transcriptViewModel (阅读视图派生模型 §5.1, §6.2)", () => {
  it("returns empty array for empty segments", () => {
    expect(deriveReadingBlocks([])).toEqual([]);
  });

  it("merges consecutive short-gap segments from the same speaker and epoch", () => {
    const segments: TranscriptSegment[] = [
      {
        id: "seg-1",
        order: 1,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 0,
        end_ms: 3000,
        text: "各位同事好，",
        source_epoch: 1,
      },
      {
        id: "seg-2",
        order: 2,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 3500, // gap = 500ms <= 1200ms
        end_ms: 7000,
        text: "今天讨论技术架构方案。",
        source_epoch: 1,
      },
    ];

    const blocks = deriveReadingBlocks(segments);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]?.segment_ids).toEqual(["seg-1", "seg-2"]);
    expect(blocks[0]?.start_ms).toBe(0);
    expect(blocks[0]?.end_ms).toBe(7000);
    expect(blocks[0]?.text).toBe("各位同事好，今天讨论技术架构方案。");
  });

  it("inserts space between ASCII/English words when merging", () => {
    const segments: TranscriptSegment[] = [
      {
        id: "seg-e1",
        order: 1,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 0,
        end_ms: 2000,
        text: "Hello",
      },
      {
        id: "seg-e2",
        order: 2,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 2200,
        end_ms: 4000,
        text: "World",
      },
    ];

    const blocks = deriveReadingBlocks(segments);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]?.text).toBe("Hello World");
  });

  it("never merges across different speakers", () => {
    const segments: TranscriptSegment[] = [
      {
        id: "seg-1",
        order: 1,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 0,
        end_ms: 3000,
        text: "请问第一阶段什么时候发布？",
      },
      {
        id: "seg-2",
        order: 2,
        speaker_key: "spk_1",
        speaker_name: "说话人 2",
        start_ms: 3200, // very short gap
        end_ms: 6000,
        text: "预计下周二发布。",
      },
    ];

    const blocks = deriveReadingBlocks(segments);
    expect(blocks).toHaveLength(2);
    expect(blocks[0]?.speaker_key).toBe("spk_0");
    expect(blocks[1]?.speaker_key).toBe("spk_1");
  });

  it("never merges across different source epochs", () => {
    const segments: TranscriptSegment[] = [
      {
        id: "seg-1",
        order: 1,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 0,
        end_ms: 3000,
        text: "断线前的内容",
        source_epoch: 1,
      },
      {
        id: "seg-2",
        order: 2,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 3200,
        end_ms: 6000,
        text: "重连后的内容",
        source_epoch: 2,
      },
    ];

    const blocks = deriveReadingBlocks(segments);
    expect(blocks).toHaveLength(2);
  });

  it("splits blocks when gap exceeds maxGapMs threshold", () => {
    const segments: TranscriptSegment[] = [
      {
        id: "seg-1",
        order: 1,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 0,
        end_ms: 3000,
        text: "第一句话",
      },
      {
        id: "seg-2",
        order: 2,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 5000, // gap = 2000ms > default 1200ms
        end_ms: 8000,
        text: "长停顿后的第二句话",
      },
    ];

    const blocks = deriveReadingBlocks(segments);
    expect(blocks).toHaveLength(2);
  });

  it("splits blocks when total duration exceeds maxDurationMs", () => {
    const segments: TranscriptSegment[] = [
      {
        id: "seg-1",
        order: 1,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 0,
        end_ms: 10000,
        text: "第一长段",
      },
      {
        id: "seg-2",
        order: 2,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 10500,
        end_ms: 22000, // 22000 - 0 = 22s > 15s max
        text: "第二长段",
      },
    ];

    const blocks = deriveReadingBlocks(segments);
    expect(blocks).toHaveLength(2);
  });

  it("propagates starredIds into block.isStarred", () => {
    const segments: TranscriptSegment[] = [
      {
        id: "seg-1",
        order: 1,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 0,
        end_ms: 2000,
        text: "普通段落",
      },
      {
        id: "seg-2",
        order: 2,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 2200,
        end_ms: 4000,
        text: "重点结论段落",
      },
    ];

    const starred = new Set(["seg-2"]);
    const blocks = deriveReadingBlocks(segments, starred);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]?.isStarred).toBe(true);
  });

  it("allows tracing block back to original segments via getSegmentsForBlock", () => {
    const segments: TranscriptSegment[] = [
      {
        id: "seg-1",
        order: 1,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 0,
        end_ms: 2000,
        text: "片段 1",
      },
      {
        id: "seg-2",
        order: 2,
        speaker_key: "spk_0",
        speaker_name: "说话人 1",
        start_ms: 2200,
        end_ms: 4000,
        text: "片段 2",
      },
    ];

    const blocks = deriveReadingBlocks(segments);
    const sourceSegments = getSegmentsForBlock(blocks[0]!, segments);
    expect(sourceSegments).toHaveLength(2);
    expect(sourceSegments[0]?.id).toBe("seg-1");
    expect(sourceSegments[1]?.id).toBe("seg-2");
  });
});
