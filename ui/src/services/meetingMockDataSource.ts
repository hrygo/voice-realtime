import type {
  MeetingDetail,
  MeetingEventEnvelope,
  MeetingListResponse,
  MeetingMinutesVersion,
  MeetingSpeaker,
  TranscriptResponse,
} from "../contracts/meetingContract";
import {
  mockMeetingDetailRecording,
  mockMeetingSummaryCompleted,
  mockMeetingSummaryRecording,
  mockMinutesCompleted,
  mockSegments,
} from "../test/fixtures/meetingFixtures";
import {
  happyPathSequence,
  healthErrorSequence,
  meetingSwitchSequence,
  revisionGapSequence,
} from "../test/fixtures/meetingEventSequences";
import type { MeetingDataSource, Unsubscribe } from "./meetingDataSource";

export interface MockOptions {
  scenario?: "happy_path" | "partial" | "revision_gap" | "meeting_switch" | "error";
  delayMs?: number;
  duplicate?: boolean;
  disconnectAfter?: number;
}

export class MeetingMockDataSource implements MeetingDataSource {
  private options: MockOptions;
  private subscribers = new Set<(event: MeetingEventEnvelope) => void>();
  private activeTimers: Array<ReturnType<typeof setTimeout>> = [];

  constructor(options: MockOptions = {}) {
    this.options = {
      scenario: "happy_path",
      delayMs: 100,
      duplicate: false,
      ...options,
    };
  }

  setOptions(options: Partial<MockOptions>) {
    this.options = { ...this.options, ...options };
  }

  async fetchMeetings(_cursor?: string | null, _limit = 20): Promise<MeetingListResponse> {
    await this.delay();
    return {
      items: [mockMeetingSummaryRecording, mockMeetingSummaryCompleted],
      next_cursor: null,
    };
  }

  async fetchMeeting(meetingId: string): Promise<MeetingDetail> {
    await this.delay();
    return {
      ...mockMeetingDetailRecording,
      id: meetingId,
    };
  }

  async updateMeetingTitle(meetingId: string, title: string): Promise<MeetingDetail> {
    await this.delay();
    return {
      ...mockMeetingDetailRecording,
      id: meetingId,
      title,
    };
  }

  async generateMeetingTitle(meetingId: string): Promise<MeetingDetail> {
    await this.delay();
    return {
      ...mockMeetingDetailRecording,
      id: meetingId,
      title: "AI 生成的智能会议标题",
    };
  }

  async fetchTranscript(meetingId: string): Promise<TranscriptResponse> {
    await this.delay();
    return {
      meeting_id: meetingId,
      transcript_revision: 2,
      content_revision: 2,
      segments: [...mockSegments],
    };
  }

  async updateSpeakerName(
    _meetingId: string,
    speakerKey: string,
    displayName: string,
  ): Promise<MeetingSpeaker> {
    await this.delay();
    return {
      speaker_key: speakerKey,
      default_label: speakerKey,
      display_name: displayName,
      updated_at: new Date().toISOString(),
    };
  }

  async generateMinutes(
    meetingId: string,
    _idempotencyKey?: string,
  ): Promise<MeetingMinutesVersion> {
    await this.delay();
    return {
      ...mockMinutesCompleted,
      meeting_id: meetingId,
      version: 1,
    };
  }

  async fetchMinutesVersion(
    meetingId: string,
    version: number,
  ): Promise<MeetingMinutesVersion> {
    await this.delay();
    return {
      ...mockMinutesCompleted,
      meeting_id: meetingId,
      version,
    };
  }

  async deleteMeeting(_meetingId: string): Promise<void> {
    await this.delay();
  }

  subscribeMeetingEvents(
    _meetingId: string,
    onEvent: (event: MeetingEventEnvelope) => void,
  ): Unsubscribe {
    this.subscribers.add(onEvent);
    this.startSequenceReplay(onEvent);

    return () => {
      this.subscribers.delete(onEvent);
    };
  }

  private startSequenceReplay(onEvent: (event: MeetingEventEnvelope) => void) {
    let sequence: MeetingEventEnvelope[] = happyPathSequence;
    if (this.options.scenario === "revision_gap") {
      sequence = revisionGapSequence;
    } else if (this.options.scenario === "meeting_switch") {
      sequence = meetingSwitchSequence;
    } else if (this.options.scenario === "error") {
      sequence = healthErrorSequence;
    }

    const interval = this.options.delayMs || 100;
    sequence.forEach((evt, idx) => {
      if (this.options.disconnectAfter && idx >= this.options.disconnectAfter) {
        return;
      }
      const timer = setTimeout(() => {
        onEvent(evt);
        if (this.options.duplicate) {
          onEvent(evt);
        }
      }, (idx + 1) * interval);
      this.activeTimers.push(timer);
    });
  }

  stopAll() {
    this.activeTimers.forEach(clearTimeout);
    this.activeTimers = [];
    this.subscribers.clear();
  }

  private delay(): Promise<void> {
    const ms = this.options.delayMs ?? 50;
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}
