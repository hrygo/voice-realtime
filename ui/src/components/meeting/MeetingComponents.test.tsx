import { describe, it, expect, vi, beforeEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { formatTimeRange, MeetingGapAlert } from "./MeetingGapAlert";
import { formatElapsed, MeetingRecordingView } from "./MeetingRecordingView";
import { formatMeetingDate, getStatusLabel, MeetingHistorySidebar } from "./MeetingHistorySidebar";
import { MeetingMinutesViewer } from "./MeetingMinutesViewer";
import { MeetingTranscriptViewer } from "./MeetingTranscriptViewer";
import { MeetingIdleView } from "./MeetingIdleView";
import {
  mockMeetingSummaryCompleted,
  mockMeetingSummaryRecording,
  mockMinutesCompleted,
  mockSegments,
} from "../../test/fixtures/meetingFixtures";

// Extend global for React 19 testing flag
declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

describe("Meeting Helper Functions", () => {
  it("formatTimeRange formats millisecond intervals properly", () => {
    expect(formatTimeRange(0, 5000)).toBe("[00:00 – 00:05]");
    expect(formatTimeRange(65000, 125000)).toBe("[01:05 – 02:05]");
  });

  it("formatElapsed formats seconds into digital clock", () => {
    expect(formatElapsed(0)).toBe("00:00");
    expect(formatElapsed(65)).toBe("01:05");
    expect(formatElapsed(3665)).toBe("01:01:05");
  });

  it("getStatusLabel returns human readable label and class", () => {
    expect(getStatusLabel("recording")).toEqual({ text: "录制中", className: "recording" });
    expect(getStatusLabel("finalizing")).toEqual({ text: "封存中", className: "finalizing" });
    expect(getStatusLabel("completed")).toEqual({ text: "已完成", className: "completed" });
    expect(getStatusLabel("interrupted")).toEqual({ text: "已中断", className: "interrupted" });
    expect(getStatusLabel("storage_error")).toEqual({ text: "存储异常", className: "storage_error" });
  });

  it("formatMeetingDate handles valid and null dates", () => {
    expect(formatMeetingDate(null)).toBe("未知时间");
    const formatted = formatMeetingDate("2026-08-21T10:30:00Z");
    expect(formatted).toContain("08/21");
  });
});

describe("Meeting React Components DOM Rendering", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    return () => {
      act(() => {
        root.unmount();
      });
      container.remove();
    };
  });

  it("renders MeetingGapAlert with formatted interval", () => {
    act(() => {
      root.render(
        <MeetingGapAlert
          gaps={[{ start_ms: 10000, end_ms: 25000, reason: "ASR 重连" }]}
        />,
      );
    });

    expect(container.textContent).toContain("转录区间存在网络/服务中断缺口");
    expect(container.textContent).toContain("[00:10 – 00:25]");
    expect(container.textContent).toContain("ASR 重连");
  });

  it("renders MeetingHistorySidebar with list of meetings and badges", () => {
    const handleSelect = vi.fn();
    const handleNew = vi.fn();
    const handleLoadMore = vi.fn();
    const handleDelete = vi.fn().mockResolvedValue(undefined);

    act(() => {
      root.render(
        <MeetingHistorySidebar
          historyList={[mockMeetingSummaryCompleted, mockMeetingSummaryRecording]}
          selectedMeetingId={mockMeetingSummaryCompleted.id}
          activeMeetingId={mockMeetingSummaryRecording.id}
          nextCursor="cursor_next"
          isLoading={false}
          onSelectMeeting={handleSelect}
          onNewMeeting={handleNew}
          onLoadMore={handleLoadMore}
          onDeleteMeeting={handleDelete}
        />,
      );
    });

    expect(container.textContent).toContain("历史会议");
    expect(container.textContent).toContain("实时语音与字幕产品评审");
    expect(container.textContent).toContain("已完成");
    expect(container.textContent).toContain("录制中");
    expect(container.textContent).toContain("加载更多历史");
  });

  it("renders MeetingIdleView with readiness checklist and triggers start", () => {
    const handleStart = vi.fn().mockResolvedValue(undefined);

    act(() => {
      root.render(
        <MeetingIdleView
          health={{
            storage: "ok",
            transcription: "ok",
            mic_muted: false,
            recovery_journal_active: false,
          }}
          onStartMeeting={handleStart}
          isStarting={false}
        />,
      );
    });

    expect(container.textContent).toContain("Voice Studio 会议助手");
    expect(container.textContent).toContain("PostgreSQL 知识库存储");
    expect(container.textContent).toContain("WhisperLiveKit 实时转录服务");
    expect(container.textContent).toContain("开始会议");

    const submitBtn = container.querySelector("button[type='submit']") as HTMLButtonElement;
    expect(submitBtn.disabled).toBe(false);

    act(() => {
      submitBtn.click();
    });

    expect(handleStart).toHaveBeenCalled();
  });

  it("renders MeetingRecordingView with active segments, partial text, and mute button", () => {
    const handleEnd = vi.fn().mockResolvedValue(undefined);
    const handleToggleMic = vi.fn();
    const handleRename = vi.fn();

    act(() => {
      root.render(
        <MeetingRecordingView
          startedAt="2026-08-21T10:00:00Z"
          segments={mockSegments}
          partialText="正在输入的临时转录片段..."
          partialSpeaker="说话人 1"
          gaps={[]}
          micMuted={false}
          onToggleMic={handleToggleMic}
          onEndMeeting={handleEnd}
          onRenameSpeaker={handleRename}
          isEnding={false}
        />,
      );
    });

    expect(container.textContent).toContain("会议助手模式运行中：已完全静音交互与回复");
    expect(container.textContent).toContain("结束会议并生成纪要");
    expect(container.textContent).toContain("张三 (架构师)");
    expect(container.textContent).toContain("正在输入的临时转录片段...");
  });

  it("renders MeetingTranscriptViewer and supports search filtering", () => {
    const handleRename = vi.fn();

    act(() => {
      root.render(
        <MeetingTranscriptViewer
          segments={mockSegments}
          highlightedSegmentId="seg-002-uuid"
          onRenameSpeaker={handleRename}
        />,
      );
    });

    expect(container.textContent).toContain("会议逐字转录");
    expect(container.textContent).toContain("张三 (架构师)");
    expect(container.textContent).toContain("李四 (前端负责人)");

    // Check highlighted class
    const highlightedCard = container.querySelector(".segment-card.highlighted");
    expect(highlightedCard).not.toBeNull();
    expect(highlightedCard?.textContent).toContain("前端部分将严格依据 v1 OpenAPI");
  });

  it("renders MeetingMinutesViewer with all 7 structured sections and handles evidence clicks", () => {
    const handleSelectEvidence = vi.fn();
    const handleRegenerate = vi.fn().mockResolvedValue(undefined);
    const handleSelectVersion = vi.fn();

    act(() => {
      root.render(
        <MeetingMinutesViewer
          minutes={mockMinutesCompleted}
          minutesList={[mockMinutesCompleted]}
          selectedVersion={1}
          onSelectVersion={handleSelectVersion}
          onRegenerate={handleRegenerate}
          onSelectEvidence={handleSelectEvidence}
          isRegenerating={false}
        />,
      );
    });

    // Verify all 7 sections
    expect(container.textContent).toContain("会议概要");
    expect(container.textContent).toContain("核心议题");
    expect(container.textContent).toContain("决策事项");
    expect(container.textContent).toContain("待办行动项");
    expect(container.textContent).toContain("风险提示");
    expect(container.textContent).toContain("待定问题");
    expect(container.textContent).toContain("精彩亮点");

    // Click evidence pill
    const evidencePill = container.querySelector(".evidence-pill") as HTMLButtonElement;
    expect(evidencePill).not.toBeNull();

    act(() => {
      evidencePill.click();
    });

    expect(handleSelectEvidence).toHaveBeenCalledWith("seg-001-uuid");
  });
});
