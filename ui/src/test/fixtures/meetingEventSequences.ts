import type {
  MeetingEventEnvelope,
  MeetingSnapshotPayload,
  MeetingStateChangedPayload,
  TranscriptPartialPayload,
  TranscriptReconciledPayload,
  SpeakerUpdatedPayload,
  MinutesStateChangedPayload,
  HealthChangedPayload,
  TranscriptionGapPayload,
  ResyncRequiredPayload,
} from "../../contracts/meetingContract";
import {
  mockMeetingSummaryRecording,
  mockMeetingSummaryCompleted,
  mockSegments,
  mockMinutesCompleted,
} from "./meetingFixtures";

export const happyPathSequence: MeetingEventEnvelope[] = [
  {
    contract_version: "1",
    type: "meeting_snapshot",
    event_id: "evt-seq-001",
    meeting_id: mockMeetingSummaryRecording.id,
    occurred_at: "2026-08-26T10:00:00Z",
    payload: {
      meeting: mockMeetingSummaryRecording,
      transcript_revision: 0,
      content_revision: 0,
      partial: null,
      health: {
        storage: "ok",
        transcription: "ok",
        mic_muted: false,
        recovery_journal_active: false,
      },
    } as MeetingSnapshotPayload,
  },
  {
    contract_version: "1",
    type: "transcript_partial",
    event_id: "evt-seq-002",
    meeting_id: mockMeetingSummaryRecording.id,
    occurred_at: "2026-08-26T10:00:02Z",
    payload: {
      text: "大家好，今天我们重点讨论",
      speaker_key: "spk_channel_0",
      speaker_name: "张三 (架构师)",
    } as TranscriptPartialPayload,
  },
  {
    contract_version: "1",
    type: "transcript_reconciled",
    event_id: "evt-seq-003",
    meeting_id: mockMeetingSummaryRecording.id,
    occurred_at: "2026-08-26T10:00:05Z",
    payload: {
      transcript_revision: 1,
      content_revision: 1,
      replace_from_ms: 0,
      segments: [mockSegments[0]],
    } as TranscriptReconciledPayload,
  },
  {
    contract_version: "1",
    type: "speaker_updated",
    event_id: "evt-seq-004",
    meeting_id: mockMeetingSummaryRecording.id,
    occurred_at: "2026-08-26T10:00:10Z",
    payload: {
      speaker_key: "spk_channel_1",
      display_name: "李四 (资深前端)",
      content_revision: 2,
    } as SpeakerUpdatedPayload,
  },
  {
    contract_version: "1",
    type: "transcript_reconciled",
    event_id: "evt-seq-005",
    meeting_id: mockMeetingSummaryRecording.id,
    occurred_at: "2026-08-26T10:00:15Z",
    payload: {
      transcript_revision: 2,
      content_revision: 2,
      replace_from_ms: 13000,
      segments: [
        {
          ...mockSegments[1],
          speaker_name: "李四 (资深前端)",
        },
      ],
    } as TranscriptReconciledPayload,
  },
  {
    contract_version: "1",
    type: "meeting_state_changed",
    event_id: "evt-seq-006",
    meeting_id: mockMeetingSummaryRecording.id,
    occurred_at: "2026-08-26T10:05:00Z",
    payload: {
      status: "finalizing",
      started_at: "2026-08-26T10:00:00Z",
      ended_at: "2026-08-26T10:05:00Z",
    } as MeetingStateChangedPayload,
  },
  {
    contract_version: "1",
    type: "meeting_state_changed",
    event_id: "evt-seq-007",
    meeting_id: mockMeetingSummaryRecording.id,
    occurred_at: "2026-08-26T10:05:02Z",
    payload: {
      status: "completed",
      started_at: "2026-08-26T10:00:00Z",
      ended_at: "2026-08-26T10:05:00Z",
    } as MeetingStateChangedPayload,
  },
  {
    contract_version: "1",
    type: "minutes_state_changed",
    event_id: "evt-seq-008",
    meeting_id: mockMeetingSummaryRecording.id,
    occurred_at: "2026-08-26T10:05:10Z",
    payload: {
      minutes_id: mockMinutesCompleted.id,
      version: 1,
      status: "completed",
      minutes: mockMinutesCompleted,
    } as MinutesStateChangedPayload,
  },
];

export const revisionGapSequence: MeetingEventEnvelope[] = [
  {
    contract_version: "1",
    type: "meeting_snapshot",
    event_id: "evt-gap-001",
    meeting_id: "gap-meeting-001",
    occurred_at: "2026-08-26T10:00:00Z",
    payload: {
      meeting: {
        ...mockMeetingSummaryRecording,
        id: "gap-meeting-001",
        transcript_revision: 1,
      },
      transcript_revision: 1,
      content_revision: 1,
      partial: null,
      health: { storage: "ok", transcription: "ok", mic_muted: false, recovery_journal_active: false },
    } as MeetingSnapshotPayload,
  },
  {
    contract_version: "1",
    type: "transcript_reconciled",
    event_id: "evt-gap-002",
    meeting_id: "gap-meeting-001",
    occurred_at: "2026-08-26T10:00:05Z",
    payload: {
      transcript_revision: 5, // Gap from 1 to 5
      content_revision: 2,
      replace_from_ms: 0,
      segments: mockSegments,
    } as TranscriptReconciledPayload,
  },
  {
    contract_version: "1",
    type: "resync_required",
    event_id: "evt-gap-003",
    meeting_id: "gap-meeting-001",
    occurred_at: "2026-08-26T10:00:06Z",
    payload: {
      expected_revision: 2,
      reason: "revision_gap_detected",
    } as ResyncRequiredPayload,
  },
];

export const meetingSwitchSequence: MeetingEventEnvelope[] = [
  {
    contract_version: "1",
    type: "meeting_snapshot",
    event_id: "evt-switch-001",
    meeting_id: "meeting-alpha",
    occurred_at: "2026-08-26T10:00:00Z",
    payload: {
      meeting: {
        ...mockMeetingSummaryRecording,
        id: "meeting-alpha",
        title: "会议 Alpha",
      },
      transcript_revision: 3,
      content_revision: 3,
      partial: {
        text: "Alpha partial",
        speaker_key: "e1:s1",
        speaker_name: "说话人 1",
      },
      health: { storage: "ok", transcription: "ok", mic_muted: false, recovery_journal_active: false },
    } as MeetingSnapshotPayload,
  },
  {
    contract_version: "1",
    type: "meeting_snapshot",
    event_id: "evt-switch-002",
    meeting_id: "meeting-beta",
    occurred_at: "2026-08-26T10:30:00Z",
    payload: {
      meeting: {
        ...mockMeetingSummaryCompleted,
        id: "meeting-beta",
        title: "会议 Beta",
      },
      transcript_revision: 1,
      content_revision: 1,
      partial: null,
      health: { storage: "ok", transcription: "ok", mic_muted: false, recovery_journal_active: false },
    } as MeetingSnapshotPayload,
  },
];

export const healthErrorSequence: MeetingEventEnvelope[] = [
  {
    contract_version: "1",
    type: "health_changed",
    event_id: "evt-err-001",
    meeting_id: mockMeetingSummaryRecording.id,
    occurred_at: "2026-08-26T10:02:00Z",
    payload: {
      storage: "degraded",
      transcription: "gap",
      mic_muted: false,
      recovery_journal_active: true,
    } as HealthChangedPayload,
  },
  {
    contract_version: "1",
    type: "transcription_gap",
    event_id: "evt-err-002",
    meeting_id: mockMeetingSummaryRecording.id,
    occurred_at: "2026-08-26T10:02:05Z",
    payload: {
      start_ms: 12000,
      end_ms: 15000,
      reason: "audio_buffer_dropped",
    } as TranscriptionGapPayload,
  },
];
