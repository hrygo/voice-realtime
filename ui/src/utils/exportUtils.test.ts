import { describe, it, expect } from "vitest";
import {
  generateJsonContent,
  generateMarkdownContent,
  generatePlainTextContent,
  generateSrtContent,
  msToReadableTime,
  msToSrtTimestamp,
} from "./exportUtils";
import {
  mockMeetingDetailCompleted,
  mockMinutesCompleted,
  mockSegments,
} from "../test/fixtures/meetingFixtures";

describe("exportUtils", () => {
  it("converts milliseconds to standard SRT timestamp format", () => {
    expect(msToSrtTimestamp(0)).toBe("00:00:00,000");
    expect(msToSrtTimestamp(15420)).toBe("00:00:15,420");
    expect(msToSrtTimestamp(3665123)).toBe("01:01:05,123");
  });

  it("converts milliseconds to readable time MM:SS", () => {
    expect(msToReadableTime(0)).toBe("00:00");
    expect(msToReadableTime(65000)).toBe("01:05");
  });

  it("generates standard SRT content", () => {
    const srt = generateSrtContent(mockSegments);
    expect(srt).toContain("1\n00:00:00,000 --> 00:00:12,450\n[张三 (架构师)] 大家好");
    expect(srt).toContain("2\n00:00:13,000 --> 00:00:24,800\n[李四 (前端负责人)] 前端部分");
  });

  it("generates structured plain text content", () => {
    const txt = generatePlainTextContent(
      mockMeetingDetailCompleted,
      mockSegments,
      mockMinutesCompleted,
    );
    expect(txt).toContain("会议主题：实时语音与字幕产品评审");
    expect(txt).toContain("【AI 会议纪要】");
    expect(txt).toContain("概要：");
    expect(txt).toContain("核心议题：");
    expect(txt).toContain("【会议转录记录】");
  });

  it("generates markdown content with tables and checklist", () => {
    const md = generateMarkdownContent(
      mockMeetingDetailCompleted,
      mockSegments,
      { ...mockMinutesCompleted, content_markdown: null },
    );
    expect(md).toContain("# 会议纪要：实时语音与字幕产品评审");
    expect(md).toContain("## 1. 会议概要");
    expect(md).toContain("## 2. 核心议题");
    expect(md).toContain("## 3. 决策事项");
    expect(md).toContain("## 4. 待办行动项");
    expect(md).toContain("| 时间 | 说话人 | 转录内容 |");
  });

  it("generates json export content", () => {
    const jsonStr = generateJsonContent(
      mockMeetingDetailCompleted,
      mockSegments,
      mockMinutesCompleted,
    );
    const parsed = JSON.parse(jsonStr);
    expect(parsed.meeting.id).toBe(mockMeetingDetailCompleted.id);
    expect(parsed.segments).toHaveLength(4);
    expect(parsed.minutes.id).toBe(mockMinutesCompleted.id);
  });
});
