import type {
  MeetingDetail,
  MeetingEventEnvelope,
  MeetingListResponse,
  MeetingMinutesVersion,
  MeetingSpeaker,
  TranscriptResponse,
} from "../contracts/meetingContract";
import { meetingApi } from "./meetingApi";

export type Unsubscribe = () => void;

export interface MeetingDataSource {
  fetchMeetings(cursor?: string | null, limit?: number): Promise<MeetingListResponse>;
  fetchMeeting(meetingId: string): Promise<MeetingDetail>;
  updateMeetingTitle(meetingId: string, title: string): Promise<MeetingDetail>;
  generateMeetingTitle(meetingId: string): Promise<MeetingDetail>;
  fetchTranscript(meetingId: string): Promise<TranscriptResponse>;
  updateSpeakerName(meetingId: string, speakerKey: string, displayName: string): Promise<MeetingSpeaker>;
  generateMinutes(meetingId: string, idempotencyKey?: string): Promise<MeetingMinutesVersion>;
  fetchMinutesVersion(meetingId: string, version: number): Promise<MeetingMinutesVersion>;
  deleteMeeting(meetingId: string): Promise<void>;
  subscribeMeetingEvents(meetingId: string, onEvent: (event: MeetingEventEnvelope) => void): Unsubscribe;
}

export class BackendMeetingDataSource implements MeetingDataSource {
  fetchMeetings(cursor?: string | null, limit?: number): Promise<MeetingListResponse> {
    return meetingApi.fetchMeetings(cursor, limit);
  }
  fetchMeeting(meetingId: string): Promise<MeetingDetail> {
    return meetingApi.fetchMeeting(meetingId);
  }
  updateMeetingTitle(meetingId: string, title: string): Promise<MeetingDetail> {
    return meetingApi.updateMeetingTitle(meetingId, title);
  }
  generateMeetingTitle(meetingId: string): Promise<MeetingDetail> {
    return meetingApi.generateMeetingTitle(meetingId);
  }
  fetchTranscript(meetingId: string): Promise<TranscriptResponse> {
    return meetingApi.fetchTranscript(meetingId);
  }
  updateSpeakerName(meetingId: string, speakerKey: string, displayName: string): Promise<MeetingSpeaker> {
    return meetingApi.updateSpeakerName(meetingId, speakerKey, displayName);
  }
  generateMinutes(meetingId: string, idempotencyKey?: string): Promise<MeetingMinutesVersion> {
    return meetingApi.generateMinutes(meetingId, idempotencyKey);
  }
  fetchMinutesVersion(meetingId: string, version: number): Promise<MeetingMinutesVersion> {
    return meetingApi.fetchMinutesVersion(meetingId, version);
  }
  deleteMeeting(meetingId: string): Promise<void> {
    return meetingApi.deleteMeeting(meetingId);
  }
  subscribeMeetingEvents(_meetingId: string, _onEvent: (event: MeetingEventEnvelope) => void): Unsubscribe {
    return () => {};
  }
}

export const defaultBackendDataSource = new BackendMeetingDataSource();
