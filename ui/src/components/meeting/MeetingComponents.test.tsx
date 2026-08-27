import { describe, it, expect, vi, beforeEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { formatTimeRange, MeetingGapAlert } from "./MeetingGapAlert";
import { formatElapsed, MeetingRecordingView } from "./MeetingRecordingView";
import { formatMeetingDate, getStatusLabel, MeetingHistorySidebar } from "./MeetingHistorySidebar";
import { MeetingMinutesViewer } from "./MeetingMinutesViewer";
import { MeetingTranscriptViewer } from "./MeetingTranscriptViewer";
import { MeetingIdleView } from "./MeetingIdleView";
import { MeetingDetailView } from "./MeetingDetailView";
import { MarkdownRenderer } from "./MarkdownRenderer";
import {
  mockMeetingDetailCompleted,
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
    const handleReturnToActive = vi.fn();
    const handleNew = vi.fn();
    const handleLoadMore = vi.fn();
    const handleDelete = vi.fn().mockResolvedValue(undefined);

    const handleRefresh = vi.fn();

    act(() => {
      root.render(
        <MeetingHistorySidebar
          historyList={[mockMeetingSummaryCompleted, mockMeetingSummaryRecording]}
          selectedMeetingId={mockMeetingSummaryCompleted.id}
          activeMeetingId={mockMeetingSummaryRecording.id}
          activeMeetingTitle="当前正在进行的评审会"
          activeStatus="recording"
          activeStartedAt="2026-08-21T10:00:00Z"
          activeSegmentsCount={5}
          nextCursor="cursor_next"
          isLoading={false}
          onSelectMeeting={handleSelect}
          onReturnToActive={handleReturnToActive}
          onNewMeeting={handleNew}
          onRefresh={handleRefresh}
          onLoadMore={handleLoadMore}
          onDeleteMeeting={handleDelete}
        />,
      );
    });

    expect(container.textContent).toContain("历史会议");
    expect(container.textContent).toContain("返回当前会议");
    expect(container.textContent).toContain("当前正在进行的评审会");
    expect(container.textContent).toContain("5 个转录段落");
    expect(container.textContent).toContain("实时语音与字幕产品评审");
    expect(container.textContent).toContain("已完成");
    expect(container.textContent).toContain("加载更多历史");

    expect(container.querySelector(".meeting-sidebar-group-status")).not.toBeNull();
    expect(container.querySelector(".meeting-sidebar-group-controls")).not.toBeNull();
    expect(container.querySelector(".meeting-sidebar-group-history")).not.toBeNull();

    // Click refresh button
    const refreshBtn = container.querySelector(".btn-refresh-history") as HTMLButtonElement;
    expect(refreshBtn).not.toBeNull();
    act(() => {
      refreshBtn.click();
    });
    expect(handleRefresh).toHaveBeenCalledTimes(1);

    // Click pinned active card
    const pinnedCard = container.querySelector(".pinned-active-meeting") as HTMLDivElement;
    expect(pinnedCard).not.toBeNull();
    act(() => {
      pinnedCard.click();
    });
    expect(handleReturnToActive).toHaveBeenCalledTimes(1);

    // Click header return button
    const returnHeaderBtn = container.querySelector(".btn-return-active-header") as HTMLButtonElement;
    expect(returnHeaderBtn).not.toBeNull();
    act(() => {
      returnHeaderBtn.click();
    });
    expect(handleReturnToActive).toHaveBeenCalledTimes(2);
  });

  it("renders 2026 collapsible rail and edge handle, supporting toggle and quick actions", () => {
    const handleToggle = vi.fn();
    const handleSelect = vi.fn();
    const handleReturn = vi.fn();
    const handleNew = vi.fn();

    act(() => {
      root.render(
        <MeetingHistorySidebar
          historyList={[mockMeetingSummaryCompleted, mockMeetingSummaryRecording]}
          selectedMeetingId={null}
          activeMeetingId={mockMeetingSummaryRecording.id}
          activeMeetingTitle="架构评审会"
          activeStatus="recording"
          activeStartedAt="2026-08-21T10:00:00Z"
          isCollapsed={true}
          isLoading={false}
          onToggleCollapse={handleToggle}
          onSelectMeeting={handleSelect}
          onReturnToActive={handleReturn}
          onNewMeeting={handleNew}
          nextCursor={null}
          onLoadMore={vi.fn()}
          onDeleteMeeting={vi.fn().mockResolvedValue(undefined)}
        />,
      );
    });

    // 1. Edge handle presence and toggle
    const edgeHandle = container.querySelector(".sidebar-edge-toggle-handle") as HTMLElement;
    expect(edgeHandle).not.toBeNull();
    act(() => {
      edgeHandle.click();
    });
    expect(handleToggle).toHaveBeenCalledTimes(1);

    // 2. Collapsed expand button
    const expandBtn = container.querySelector(".btn-expand-sidebar") as HTMLButtonElement;
    expect(expandBtn).not.toBeNull();
    act(() => {
      expandBtn.click();
    });
    expect(handleToggle).toHaveBeenCalledTimes(2);

    // 3. Mini wave soundwave pulse for recording
    const pulseCard = container.querySelector(".meeting-sidebar-collapsed-pulse.is-recording") as HTMLElement;
    expect(pulseCard).not.toBeNull();
    expect(container.querySelector(".collapsed-mini-wave")).not.toBeNull();
    act(() => {
      pulseCard.click();
    });
    expect(handleReturn).toHaveBeenCalledTimes(1);

    // 4. Recent meetings rail & hover flyouts
    const recentRail = container.querySelector(".collapsed-recent-rail");
    expect(recentRail).not.toBeNull();
    const recentItems = container.querySelectorAll(".collapsed-recent-item");
    expect(recentItems.length).toBeGreaterThan(0);
    expect(container.querySelector(".collapsed-item-flyout")).not.toBeNull();

    // Click recent item (first item is mockMeetingSummaryCompleted)
    act(() => {
      (recentItems[0] as HTMLElement).click();
    });
    expect(handleSelect).toHaveBeenCalledWith(mockMeetingSummaryCompleted.id);
  });

  it("delegates history deletion without opening a second confirmation modal", () => {
    const handleDelete = vi.fn().mockResolvedValue(undefined);

    act(() => {
      root.render(
        <MeetingHistorySidebar
          historyList={[mockMeetingSummaryCompleted]}
          selectedMeetingId={null}
          activeMeetingId={null}
          activeStatus="idle"
          isLoading={false}
          onSelectMeeting={vi.fn()}
          onReturnToActive={vi.fn()}
          onNewMeeting={vi.fn()}
          nextCursor={null}
          onLoadMore={vi.fn()}
          onDeleteMeeting={handleDelete}
        />,
      );
    });

    const deleteButton = container.querySelector(".history-delete-btn") as HTMLButtonElement;
    expect(deleteButton).not.toBeNull();

    act(() => {
      deleteButton.click();
    });

    expect(handleDelete).toHaveBeenCalledTimes(1);
    expect(handleDelete).toHaveBeenCalledWith(mockMeetingSummaryCompleted.id);
    expect(container.querySelector(".modal-dialog")).toBeNull();
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

  it("renders MeetingRecordingView with active segments, partial text, and controls", () => {
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

    expect(container.textContent).toContain("已确认 4 个发言片段");
    expect(container.textContent).toContain("时序视图");
    expect(container.textContent).toContain("阅读视图");
    expect(container.textContent).toContain("结束会议并生成纪要");
    expect(container.textContent).toContain("张三 (架构师)");
    expect(container.textContent).toContain("正在输入的临时转录片段...");

    // Switch to reading view
    const readingBtn = container.querySelector("button[title*='阅读视图']") as HTMLButtonElement;
    expect(readingBtn).not.toBeNull();
    act(() => {
      readingBtn.click();
    });
    expect(container.querySelector(".reading-block-card")).not.toBeNull();
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
    expect(container.querySelector(".pane-actions-group")).not.toBeNull();
    expect(container.querySelector("button[title='复制当前筛选的全部转录文本']")).toBeNull();

    // Check highlighted class
    const highlightedCard = container.querySelector(".segment-card.highlighted");
    expect(highlightedCard).not.toBeNull();
    expect(highlightedCard?.textContent).toContain("前端部分将严格依据 v1 OpenAPI");
  });

  it("uses the clipboard fallback when copying a meeting transcript segment", async () => {
    const originalClipboard = navigator.clipboard;
    const originalExecCommand = document.execCommand;
    const execCommand = vi.fn().mockReturnValue(true);

    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: execCommand,
    });

    try {
      act(() => {
        root.render(
          <MeetingTranscriptViewer
            segments={mockSegments}
            highlightedSegmentId={null}
            onRenameSpeaker={vi.fn()}
          />,
        );
      });

      const copyButton = container.querySelector(".segment-copy-btn") as HTMLButtonElement;
      expect(copyButton).not.toBeNull();

      await act(async () => {
        copyButton.click();
        await Promise.resolve();
      });

      expect(execCommand).toHaveBeenCalledWith("copy");
      expect(container.querySelector("textarea[data-clipboard-fallback]")).toBeNull();
    } finally {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: originalClipboard,
      });
      Object.defineProperty(document, "execCommand", {
        configurable: true,
        value: originalExecCommand,
      });
    }
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

    // Verify all sections and title badge
    expect(container.textContent).toContain("AI 纪要主题提炼");
    expect(container.textContent).toContain("实时语音与字幕产品评审");
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

  it("renders MeetingMinutesViewer empty state and handles generate click when minutes is null", () => {
    const handleRegenerate = vi.fn().mockResolvedValue(undefined);

    act(() => {
      root.render(
        <MeetingMinutesViewer
          minutes={null}
          minutesList={[]}
          selectedVersion={null}
          onSelectVersion={vi.fn()}
          onRegenerate={handleRegenerate}
          onSelectEvidence={vi.fn()}
          isRegenerating={false}
        />,
      );
    });

    expect(container.textContent).toContain("尚未生成 AI 结构化纪要");
    const generateBtn = Array.from(container.querySelectorAll("button")).find((btn) =>
      btn.textContent?.includes("立即生成 AI 纪要"),
    );
    expect(generateBtn).not.toBeUndefined();
    expect(generateBtn?.disabled).toBe(false);

    act(() => {
      generateBtn?.click();
    });
    expect(handleRegenerate).toHaveBeenCalledTimes(1);
  });

  it("renders MeetingMinutesViewer failed state with error message and handles retry", () => {
    const handleRegenerate = vi.fn().mockResolvedValue(undefined);
    const failedMinutes = {
      ...mockMinutesCompleted,
      status: "failed" as const,
      content_json: null,
      error_code: "summary_unavailable",
      error_message: "AI 纪要服务暂不可用，请检查 LLM",
    };

    act(() => {
      root.render(
        <MeetingMinutesViewer
          minutes={failedMinutes}
          minutesList={[failedMinutes]}
          selectedVersion={1}
          onSelectVersion={vi.fn()}
          onRegenerate={handleRegenerate}
          onSelectEvidence={vi.fn()}
          isRegenerating={false}
        />,
      );
    });

    expect(container.textContent).toContain("AI 纪要生成失败");
    expect(container.textContent).toContain("AI 纪要服务暂不可用，请检查 LLM");

    const retryBtn = Array.from(container.querySelectorAll("button")).find((btn) =>
      btn.textContent?.includes("重试生成"),
    );
    expect(retryBtn).not.toBeUndefined();

    act(() => {
      retryBtn?.click();
    });
    expect(handleRegenerate).toHaveBeenCalledTimes(1);
  });

  it("renders MeetingDetailView top navigation breadcrumb and supports return to active meeting", () => {
    const handleReturnToActive = vi.fn();
    const handleUpdateTitle = vi.fn().mockResolvedValue(undefined);
    const handleRenameSpeaker = vi.fn();
    const handleRegenerate = vi.fn().mockResolvedValue(undefined);
    const handleDelete = vi.fn().mockResolvedValue(undefined);

    act(() => {
      root.render(
        <MeetingDetailView
          meeting={mockMeetingDetailCompleted}
          segments={mockSegments}
          minutes={mockMinutesCompleted}
          minutesList={[mockMinutesCompleted]}
          selectedMinutesVersion={1}
          onSelectMinutesVersion={vi.fn()}
          onUpdateTitle={handleUpdateTitle}
          onGenerateTitle={vi.fn().mockResolvedValue(undefined)}
          onRenameSpeaker={handleRenameSpeaker}
          onRegenerateMinutes={handleRegenerate}
          onDeleteMeeting={handleDelete}
          isMeetingActive={true}
          activeMeetingTitle="进行中的架构评审"
          onReturnToActive={handleReturnToActive}
        />,
      );
    });

    expect(container.textContent).toContain("返回正在进行的会议（进行中的架构评审）");
    expect(container.textContent).toContain("4 段发言");

    const splitGrid = container.querySelector(".dual-pane-grid") as HTMLDivElement;
    expect(splitGrid).not.toBeNull();
    expect(splitGrid.style.getPropertyValue("--meeting-split-percent")).toBe("48%");
    expect(splitGrid.style.gridTemplateColumns).toBe("");

    const backBtn = container.querySelector(".detail-back-btn.is-live-return") as HTMLButtonElement;
    expect(backBtn).not.toBeNull();

    act(() => {
      backBtn.click();
    });

    expect(handleReturnToActive).toHaveBeenCalledTimes(1);
  });

  it("generates an AI title through one callback without issuing a second title update", async () => {
    const handleGenerateTitle = vi.fn().mockResolvedValue(undefined);
    const handleUpdateTitle = vi.fn().mockResolvedValue(undefined);

    act(() => {
      root.render(
        <MeetingDetailView
          meeting={mockMeetingDetailCompleted}
          segments={mockSegments}
          minutes={mockMinutesCompleted}
          minutesList={[mockMinutesCompleted]}
          selectedMinutesVersion={1}
          onSelectMinutesVersion={vi.fn()}
          onUpdateTitle={handleUpdateTitle}
          onGenerateTitle={handleGenerateTitle}
          onRenameSpeaker={vi.fn()}
          onRegenerateMinutes={vi.fn().mockResolvedValue(undefined)}
          onDeleteMeeting={vi.fn().mockResolvedValue(undefined)}
        />,
      );
    });

    const button = container.querySelector(".ai-title-gen-btn") as HTMLButtonElement;
    await act(async () => {
      button.click();
      await Promise.resolve();
    });

    expect(handleGenerateTitle).toHaveBeenCalledTimes(1);
    expect(handleUpdateTitle).not.toHaveBeenCalled();
  });

  it("renders MarkdownRenderer with fenced code blocks and copy button", () => {
    const markdownSample = "### 代码示例\n\n```python\ndef hello_world():\n    print('Hello Voice Studio')\n```\n\n- [ ] 待办事项";

    act(() => {
      root.render(<MarkdownRenderer content={markdownSample} />);
    });

    expect(container.textContent).toContain("代码示例");
    expect(container.textContent).toContain("PYTHON");
    expect(container.textContent).toContain("def hello_world():");
    expect(container.textContent).toContain("📋 复制");
    expect(container.textContent).toContain("待办事项");
  });

  it("renders speaker distribution bar with percentage chips and supports filtering", () => {
    const handleRename = vi.fn();

    act(() => {
      root.render(
        <MeetingTranscriptViewer
          segments={mockSegments}
          highlightedSegmentId={null}
          onRenameSpeaker={handleRename}
        />,
      );
    });

    expect(container.textContent).toContain("张三 (架构师)");
    expect(container.textContent).toContain("李四 (前端负责人)");

    // Speaker distribution bar and chips
    const distributionBar = container.querySelector(".speaker-distribution-bar");
    expect(distributionBar).not.toBeNull();

    const chips = container.querySelectorAll(".speaker-stat-chip");
    expect(chips.length).toBeGreaterThanOrEqual(2);

    // Click a speaker chip to filter
    const firstChip = chips[0] as HTMLButtonElement;
    act(() => {
      firstChip.click();
    });
    expect(firstChip.classList.contains("selected")).toBe(true);
  });
});
