import { beforeEach, describe, expect, it } from "vitest";

import type { TranscriptSegment } from "../contracts/meetingContract";
import { mockMinutesCompleted, mockSegments } from "../test/fixtures/meetingFixtures";
import { useMeetingStore } from "./meetingStore";

describe("meetingStore", () => {
  beforeEach(() => {
    useMeetingStore.getState().resetActiveSession();
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
          end_ms: 10000,
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

      // 新事件：replace_from_ms = 10000 (即 seg-1 保留，seg-2 被替换)
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
        partial: "实时转录预览文字",
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
  });
});

