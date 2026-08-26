import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TranscriptSegment } from "../contracts/meetingContract";
import {
  mockMeetingDetailCompleted,
  mockMeetingSummaryCompleted,
  mockMeetingSummaryRecording,
  mockMinutesCompleted,
  mockSegments,
} from "../test/fixtures/meetingFixtures";
import { meetingApi } from "../services/meetingApi";
import { useMeetingStore } from "./meetingStore";

describe("meetingStore", () => {
  beforeEach(() => {
    useMeetingStore.getState().resetActiveSession();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe("reconcileTranscript (§8 转录对账算法)", () => {
    it("preserves stable history before replace_from_ms and replaces overlapping window", () => {
      // 初始有 2 段
      const initialSegments: TranscriptSegment[] = [
        {
          id: "seg-1",
          order: 1,
          speaker_key: "spk_0",
          speaker_name: "说话人 1",
          start_ms: 0,
          end_ms: 9500,
          text: "第一句话已稳定确认",
        },
        {
          id: "seg-2",
          order: 2,
          speaker_key: "spk_0",
          speaker_name: "说话人 1",
          start_ms: 10000,
          end_ms: 20000,
          text: "第二句话（待修订版本）",
        },
      ];

      useMeetingStore.setState({
        segments: initialSegments,
        transcriptRevision: 1,
        contentRevision: 1,
        partialText: "正在输入的易失文本",
      });

      // 新事件：replace_from_ms = 10000 (即 seg-1 9500 < 10000 保留，seg-2 20000 >= 10000 被替换)
      const updatedWindow: TranscriptSegment[] = [
        {
          id: "seg-2-revised",
          order: 2,
          speaker_key: "spk_0",
          speaker_name: "说话人 1",
          start_ms: 10000,
          end_ms: 22000,
          text: "第二句话（最终修订正确版本）",
        },
        {
          id: "seg-3",
          order: 3,
          speaker_key: "spk_1",
          speaker_name: "说话人 2",
          start_ms: 22500,
          end_ms: 30000,
          text: "第三句话新产生",
        },
      ];

      useMeetingStore.getState().reconcileTranscript(10000, updatedWindow, 2, 2);

      const state = useMeetingStore.getState();
      expect(state.segments).toHaveLength(3);
      expect(state.segments[0]?.id).toBe("seg-1");
      expect(state.segments[1]?.id).toBe("seg-2-revised");
      expect(state.segments[1]?.text).toBe("第二句话（最终修订正确版本）");
      expect(state.segments[2]?.id).toBe("seg-3");
      expect(state.transcriptRevision).toBe(2);
      expect(state.contentRevision).toBe(2);
      expect(state.partialText).toBeNull(); // 已提交后清空 partial
    });

    it("stably sorts segments by start_ms and order", () => {
      const outOfOrderSegments: TranscriptSegment[] = [
        {
          id: "seg-b",
          order: 2,
          speaker_key: "spk_0",
          speaker_name: "说话人 1",
          start_ms: 5000,
          end_ms: 10000,
          text: "第二段",
        },
        {
          id: "seg-a",
          order: 1,
          speaker_key: "spk_0",
          speaker_name: "说话人 1",
          start_ms: 0,
          end_ms: 4500,
          text: "第一段",
        },
      ];

      useMeetingStore.getState().reconcileTranscript(0, outOfOrderSegments, 1, 1);
      const segments = useMeetingStore.getState().segments;
      expect(segments[0]?.id).toBe("seg-a");
      expect(segments[1]?.id).toBe("seg-b");
    });
  });

  describe("Speaker Renaming & Stale Detection (§9, §10.4)", () => {
    it("updates speaker display name across all matching segments and marks minutes stale", () => {
      useMeetingStore.setState({
        segments: [...mockSegments],
        minutes: { ...mockMinutesCompleted, source_content_revision: 5, is_stale: false },
        contentRevision: 5,
      });

      // 将 spk_channel_0 改名为 "张总"
      useMeetingStore.getState().setSpeaker("spk_channel_0", "张总", 6);

      const state = useMeetingStore.getState();
      expect(state.speakers["spk_channel_0"]?.display_name).toBe("张总");
      expect(state.segments[0]?.speaker_name).toBe("张总");
      expect(state.segments[2]?.speaker_name).toBe("张总");
      // 未修改的说话人保持不变
      expect(state.segments[1]?.speaker_name).toBe("李四 (前端负责人)");
      // 纪要由于 contentRevision(6) > source_content_revision(5) 标记为 stale
      expect(state.minutes?.is_stale).toBe(true);
    });
  });

  describe("Gaps and Health Handling (§11.2, §14.2)", () => {
    it("records transcription gaps", () => {
      useMeetingStore.getState().addGap(10000, 15000, "网络丢包中断");
      const state = useMeetingStore.getState();
      expect(state.gaps).toHaveLength(1);
      expect(state.gaps[0]?.start_ms).toBe(10000);
      expect(state.gaps[0]?.end_ms).toBe(15000);
      expect(state.gaps[0]?.reason).toBe("网络丢包中断");
    });

    it("updates health state accurately", () => {
      useMeetingStore.getState().updateHealth({
        storage: "degraded",
        recovery_journal_active: true,
      });
      const health = useMeetingStore.getState().health;
      expect(health.storage).toBe("degraded");
      expect(health.recovery_journal_active).toBe(true);
      expect(health.transcription).toBe("ok");
    });
  });

  describe("applySnapshot (§14.2)", () => {
    it("applies meeting snapshot to store", () => {
      useMeetingStore.getState().applySnapshot({
        meeting: {
          id: "m-snapshot-1",
          title: "快照测试会议",
          status: "recording",
          language: "Chinese",
          started_at: "2026-08-21T10:00:00Z",
          ended_at: null,
          transcript_revision: 10,
          content_revision: 10,
          created_at: "2026-08-21T10:00:00Z",
        },
        transcript_revision: 10,
        content_revision: 10,
        partial: {
          text: "实时转录预览文字",
          speaker_key: "e1:s1",
          speaker_name: "说话人 1",
        },
        health: {
          storage: "ok",
          transcription: "ok",
          mic_muted: false,
        },
      });

      const state = useMeetingStore.getState();
      expect(state.activeMeetingId).toBe("m-snapshot-1");
      expect(state.status).toBe("recording");
      expect(state.transcriptRevision).toBe(10);
      expect(state.partialText).toBe("实时转录预览文字");
    });

    it("normalizes the structured partial snapshot into renderable text and speaker", () => {
      useMeetingStore.getState().applySnapshot({
        meeting: {
          id: "m-partial-1",
          title: "结构化 partial 测试会议",
          status: "recording",
          language: "Chinese",
          started_at: "2026-08-21T10:00:00Z",
          ended_at: null,
          transcript_revision: 1,
          content_revision: 1,
          created_at: "2026-08-21T10:00:00Z",
        },
        transcript_revision: 1,
        content_revision: 1,
        partial: {
          text: "正在识别的结构化预览",
          speaker_key: "e1:s2",
          speaker_name: "说话人 2",
        },
      });

      const state = useMeetingStore.getState();
      expect(state.partialText).toBe("正在识别的结构化预览");
      expect(state.partialSpeaker).toBe("说话人 2");
    });

    it("does not let a stale baseline response overwrite a newer active meeting", async () => {
      vi.spyOn(meetingApi, "fetchTranscript").mockResolvedValueOnce({
        meeting_id: "m-old",
        transcript_revision: 2,
        content_revision: 2,
        segments: [
          {
            id: "old-segment",
            order: 0,
            speaker_key: "spk-old",
            speaker_name: "旧会议",
            start_ms: 0,
            end_ms: 100,
            text: "旧会议内容",
          },
        ],
      });
      vi.spyOn(meetingApi, "fetchMeeting").mockRejectedValueOnce(new Error("stale request"));
      useMeetingStore.setState({
        activeMeetingId: "m-old",
        segments: [],
      });

      const request = useMeetingStore.getState().syncBaselineTranscript("m-old");
      useMeetingStore.setState({
        activeMeetingId: "m-new",
        segments: [
          {
            id: "new-segment",
            order: 0,
            speaker_key: "spk-new",
            speaker_name: "新会议",
            start_ms: 0,
            end_ms: 100,
            text: "新会议内容",
          },
        ],
      });
      await request;

      expect(useMeetingStore.getState().segments[0]?.id).toBe("new-segment");
    });
  });

  describe("updateMeetingState", () => {
    it("updates status and activeMeetingId when meetingId is provided", () => {
      useMeetingStore
        .getState()
        .updateMeetingState("recording", "2026-08-21T10:00:00Z", null, null, "m-test-id");
      const state = useMeetingStore.getState();
      expect(state.status).toBe("recording");
      expect(state.activeMeetingId).toBe("m-test-id");
      expect(state.sessionStartedAt).toBe("2026-08-21T10:00:00Z");
    });

    it("preserves activeMeetingId when meetingId is omitted or undefined", () => {
      useMeetingStore.setState({ activeMeetingId: "existing-id" });
      useMeetingStore.getState().updateMeetingState("finalizing");
      const state = useMeetingStore.getState();
      expect(state.status).toBe("finalizing");
      expect(state.activeMeetingId).toBe("existing-id");
      expect(state.isFinalizing).toBe(true);
    });

    it("does not move the active view for a selected historical meeting event", () => {
      useMeetingStore.setState({
        activeMeetingId: "m-current",
        status: "recording",
        selectedMeetingId: "m-history",
        historyList: [
          {
            id: "m-history",
            title: "历史会议",
            status: "recording",
            language: "Chinese",
            started_at: "2026-08-21T10:00:00Z",
            ended_at: null,
            transcript_revision: 1,
            content_revision: 1,
            created_at: "2026-08-21T10:00:00Z",
          },
        ],
      });

      useMeetingStore
        .getState()
        .updateMeetingState("completed", null, "2026-08-21T10:30:00Z", null, "m-history");

      const state = useMeetingStore.getState();
      expect(state.activeMeetingId).toBe("m-current");
      expect(state.status).toBe("recording");
      expect(state.historyList[0]?.status).toBe("completed");
    });
  });

  describe("setMinutesState (§10, §14.2)", () => {
    it("handles failure state when minutesData is null and creates failed minutes record", () => {
      useMeetingStore.setState({ activeMeetingId: "m-fail-1", minutes: null });

      useMeetingStore
        .getState()
        .setMinutesState(
          1,
          "failed",
          "summary_unavailable",
          "AI 纪要服务暂不可用",
          null,
          "m-fail-1",
          "min-id-1",
        );

      const state = useMeetingStore.getState();
      expect(state.minutes).not.toBeNull();
      expect(state.minutes?.status).toBe("failed");
      expect(state.minutes?.error_code).toBe("summary_unavailable");
      expect(state.minutes?.error_message).toBe("AI 纪要服务暂不可用");
      expect(state.minutesHistory).toHaveLength(1);
      expect(state.minutesHistory[0]?.status).toBe("failed");
    });

    it("updates selectedMinutes and selectedMinutesList when viewing historical meeting", () => {
      useMeetingStore.setState({
        selectedMeetingId: "m-history-1",
        selectedMinutes: null,
        selectedMinutesList: [],
      });

      useMeetingStore
        .getState()
        .setMinutesState(
          1,
          "generating",
          null,
          null,
          null,
          "m-history-1",
          "min-h-1",
        );

      const state = useMeetingStore.getState();
      expect(state.selectedMinutes).not.toBeNull();
      expect(state.selectedMinutes?.status).toBe("generating");
      expect(state.selectedMinutesList).toHaveLength(1);
    });
  });

  describe("meeting_title_updated", () => {
    it("updates active, selected and history meeting titles atomically", () => {
      useMeetingStore.setState({
        activeMeetingId: mockMeetingSummaryRecording.id,
        activeMeeting: {
          ...mockMeetingDetailCompleted,
          id: mockMeetingSummaryRecording.id,
          title: mockMeetingSummaryRecording.title,
        },
        selectedMeetingId: mockMeetingSummaryCompleted.id,
        selectedMeeting: mockMeetingDetailCompleted,
        historyList: [mockMeetingSummaryRecording, mockMeetingSummaryCompleted],
      });

      useMeetingStore
        .getState()
        .setMeetingTitle(mockMeetingSummaryCompleted.id, "AI 新标题");

      const state = useMeetingStore.getState();
      expect(state.selectedMeeting?.title).toBe("AI 新标题");
      expect(
        state.historyList.find((meeting) => meeting.id === mockMeetingSummaryCompleted.id)?.title,
      ).toBe("AI 新标题");
      expect(state.activeMeeting?.title).toBe(mockMeetingSummaryRecording.title);
    });
  });

  describe("minutes idempotency", () => {
    it("sends an idempotency key for every user-triggered generation request", async () => {
      const generate = vi
        .spyOn(meetingApi, "generateMinutes")
        .mockResolvedValue(mockMinutesCompleted);

      await useMeetingStore.getState().triggerGenerateMinutes(mockMeetingSummaryCompleted.id);

      expect(generate).toHaveBeenCalledWith(
        mockMeetingSummaryCompleted.id,
        expect.any(String),
      );
      expect(generate.mock.calls[0]?.[1]).not.toBe("");
    });
  });

  describe("returnToActiveMeeting & selectMeeting Navigation UX", () => {
    it("returnToActiveMeeting clears selected meeting history state", () => {
      useMeetingStore.setState({
        selectedMeetingId: "m-history-1",
        selectedSegments: [...mockSegments],
        selectedMinutes: mockMinutesCompleted,
        selectedMinutesVersion: 1,
        selectedMinutesList: [mockMinutesCompleted],
      });

      useMeetingStore.getState().returnToActiveMeeting();

      const state = useMeetingStore.getState();
      expect(state.selectedMeetingId).toBeNull();
      expect(state.selectedMeeting).toBeNull();
      expect(state.selectedSegments).toHaveLength(0);
      expect(state.selectedMinutes).toBeNull();
      expect(state.selectedMinutesList).toHaveLength(0);
    });

    it("selectMeeting redirects to returnToActiveMeeting when selecting currently active recording", async () => {
      useMeetingStore.setState({
        activeMeetingId: "m-active-live",
        status: "recording",
        selectedMeetingId: "m-old-history",
      });

      await useMeetingStore.getState().selectMeeting("m-active-live");

      const state = useMeetingStore.getState();
      // Should clear selectedMeetingId rather than loading static snapshot
      expect(state.selectedMeetingId).toBeNull();
      expect(state.activeMeetingId).toBe("m-active-live");
      expect(state.status).toBe("recording");
    });
  });

  describe("Real-time History Synchronization", () => {
    it("updateMeetingState synchronizes status and times in historyList", () => {
      useMeetingStore.setState({
        activeMeetingId: "m-live-1",
        status: "recording",
        historyList: [
          {
            id: "m-live-1",
            title: "现场架构研讨会",
            status: "recording",
            language: "Chinese",
            started_at: "2026-08-23T10:00:00Z",
            ended_at: null,
            transcript_revision: 1,
            content_revision: 1,
            created_at: "2026-08-23T10:00:00Z",
          },
        ],
      });

      useMeetingStore.getState().updateMeetingState(
        "completed",
        "2026-08-23T10:00:00Z",
        "2026-08-23T10:45:00Z",
        null,
        "m-live-1",
      );

      const state = useMeetingStore.getState();
      expect(state.status).toBe("completed");
      expect(state.historyList[0]?.status).toBe("completed");
      expect(state.historyList[0]?.ended_at).toBe("2026-08-23T10:45:00Z");
    });

    it("setMinutesState synchronizes minutes in active and selected states", () => {
      useMeetingStore.setState({
        activeMeetingId: "m-live-2",
        selectedMeetingId: "m-live-2",
        selectedMeeting: {
          id: "m-live-2",
          title: "产品评审会",
          status: "completed",
          language: "Chinese",
          audio_source: "microphone",
          started_at: "2026-08-23T11:00:00Z",
          ended_at: "2026-08-23T11:30:00Z",
          transcript_revision: 5,
          content_revision: 5,
          speakers: {},
          latest_minutes: null,
          created_at: "2026-08-23T11:00:00Z",
          updated_at: "2026-08-23T11:30:00Z",
        },
      });

      useMeetingStore.getState().setMinutesState(
        1,
        "completed",
        null,
        null,
        mockMinutesCompleted,
        "m-live-2",
        "min-123",
      );

      const state = useMeetingStore.getState();
      expect(state.minutes?.status).toBe("completed");
      expect(state.selectedMeeting?.latest_minutes?.status).toBe("completed");
      expect(state.selectedMinutes?.version).toBe(1);
    });

    it("updateMeetingState prepends newly created meeting if not present in historyList", () => {
      useMeetingStore.setState({
        activeMeetingId: "m-brand-new",
        activeMeeting: {
          id: "m-brand-new",
          title: "战略规划研讨会",
          status: "recording",
          language: "Chinese",
          audio_source: "microphone",
          started_at: "2026-08-23T12:00:00Z",
          ended_at: null,
          transcript_revision: 0,
          content_revision: 0,
          speakers: {},
          created_at: "2026-08-23T12:00:00Z",
          updated_at: "2026-08-23T12:00:00Z",
        },
        historyList: [],
      });

      useMeetingStore.getState().updateMeetingState(
        "recording",
        "2026-08-23T12:00:00Z",
        null,
        null,
        "m-brand-new",
      );

      const state = useMeetingStore.getState();
      expect(state.historyList).toHaveLength(1);
      expect(state.historyList[0]?.id).toBe("m-brand-new");
      expect(state.historyList[0]?.title).toBe("战略规划研讨会");
    });

    it("resetActiveSession completely clears active and selected state for pristine new meeting view", () => {
      useMeetingStore.setState({
        activeMeetingId: "m-old-completed",
        status: "completed",
        selectedMeetingId: "m-old-completed",
        selectedMeeting: {
          id: "m-old-completed",
          title: "旧会议",
          status: "completed",
          language: "Chinese",
          audio_source: "microphone",
          started_at: "2026-08-23T10:00:00Z",
          ended_at: "2026-08-23T10:30:00Z",
          transcript_revision: 5,
          content_revision: 5,
          speakers: {},
          created_at: "2026-08-23T10:00:00Z",
          updated_at: "2026-08-23T10:30:00Z",
        },
      });

      useMeetingStore.getState().resetActiveSession();

      const state = useMeetingStore.getState();
      expect(state.activeMeetingId).toBeNull();
      expect(state.activeMeeting).toBeNull();
      expect(state.status).toBe("idle");
      expect(state.selectedMeetingId).toBeNull();
      expect(state.selectedMeeting).toBeNull();
      expect(state.isCalibrating).toBe(false);
    });

    it("applySnapshot atomically resets previous meeting segments, speakers, minutes and gaps when meeting ID switches", () => {
      useMeetingStore.setState({
        activeMeetingId: "m-first",
        status: "recording",
        segments: [
          {
            id: "seg-first-1",
            order: 1,
            speaker_key: "spk_0",
            speaker_name: "旧说话人",
            start_ms: 0,
            end_ms: 5000,
            text: "第一场会议的发言",
          },
        ],
        speakers: {
          spk_0: {
            speaker_key: "spk_0",
            default_label: "spk_0",
            display_name: "旧说话人",
            updated_at: "2026-08-26T10:00:00Z",
          },
        },
        minutes: mockMinutesCompleted,
        minutesHistory: [mockMinutesCompleted],
        activeMinutesVersion: 1,
        gaps: [{ start_ms: 1000, end_ms: 2000, reason: "audio drop" }],
        partialText: "未完成的旧输入",
        partialSpeaker: "旧说话人",
        transcriptRevision: 10,
        contentRevision: 10,
      });

      const newSnapshot = {
        meeting: {
          id: "m-second",
          title: "第二场会议",
          status: "recording" as const,
          language: "Chinese",
          started_at: "2026-08-26T11:00:00Z",
          ended_at: null,
          transcript_revision: 1,
          content_revision: 1,
          interruption_reason: null,
          created_at: "2026-08-26T11:00:00Z",
        },
        transcript_revision: 1,
        content_revision: 1,
        partial: {
          text: "新会议的首句 partial",
          speaker_key: null,
          speaker_name: null,
        },
        health: {
          storage: "ok" as const,
          transcription: "ok",
          mic_muted: false,
          recovery_journal_active: false,
        },
      };

      useMeetingStore.getState().applySnapshot(newSnapshot);

      const state = useMeetingStore.getState();
      expect(state.activeMeetingId).toBe("m-second");
      expect(state.segments).toHaveLength(0); // 原子重置
      expect(state.speakers).toEqual({}); // 原子重置
      expect(state.minutes).toBeNull(); // 原子重置
      expect(state.minutesHistory).toHaveLength(0); // 原子重置
      expect(state.activeMinutesVersion).toBeNull();
      expect(state.gaps).toHaveLength(0); // 原子重置
      expect(state.partialText).toBe("新会议的首句 partial");
      expect(state.partialSpeaker).toBeNull();
      expect(state.transcriptRevision).toBe(1);
      expect(state.contentRevision).toBe(1);
    });

    it("reconcileTranscript replaces segments ending exactly at replace_from_ms (end_ms >= replace_from_ms)", () => {
      const initialSegments: TranscriptSegment[] = [
        {
          id: "seg-exact-boundary",
          order: 1,
          speaker_key: "spk_0",
          speaker_name: "说话人 1",
          start_ms: 0,
          end_ms: 10000,
          text: "旧边界内容（结束于10000）",
        },
      ];

      useMeetingStore.setState({
        segments: initialSegments,
        transcriptRevision: 1,
        contentRevision: 1,
      });

      const newWindow: TranscriptSegment[] = [
        {
          id: "seg-replacement",
          order: 1,
          speaker_key: "spk_0",
          speaker_name: "说话人 1",
          start_ms: 10000,
          end_ms: 15000,
          text: "新替换内容",
        },
      ];

      // replace_from_ms = 10000 -> seg-exact-boundary.end_ms (10000) >= 10000 -> 被替换
      useMeetingStore.getState().reconcileTranscript(10000, newWindow, 2, 2);

      const state = useMeetingStore.getState();
      expect(state.segments).toHaveLength(1);
      expect(state.segments[0]?.id).toBe("seg-replacement");
    });
  });
});
