import { useEffect, useState } from "react";
import {
  type HealthChangedPayload,
  type MeetingEventEnvelope,
  type MeetingEventType,
  type MeetingSnapshotPayload,
  type MeetingStateChangedPayload,
  type MinutesStateChangedPayload,
  type SpeakerUpdatedPayload,
  type TranscriptionGapPayload,
  type TranscriptPartialPayload,
  type TranscriptReconciledPayload,
} from "../contracts/meetingContract";
import { useMeetingStore } from "../stores/meetingStore";
import { ReconnectingSocket, type ConnectionState } from "./useEventSocket";

export function isMeetingEventEnvelope(val: unknown): val is MeetingEventEnvelope {
  if (!val || typeof val !== "object") return false;
  const obj = val as Record<string, unknown>;
  return (
    obj.contract_version === "1" &&
    typeof obj.type === "string" &&
    typeof obj.meeting_id === "string" &&
    "payload" in obj
  );
}

type MeetingEventTargetState = {
  activeMeetingId: string | null;
  selectedMeetingId: string | null;
  selectedMeeting: { id: string } | null;
};

export function isMeetingEventRelevant(
  type: MeetingEventType,
  meetingId: string,
  state: MeetingEventTargetState,
): boolean {
  if (meetingId === state.activeMeetingId) return true;
  if (type === "minutes_state_changed" || type === "meeting_state_changed") {
    return meetingId === state.selectedMeetingId || meetingId === state.selectedMeeting?.id;
  }
  return false;
}

export function useMeetingSocket(url = "/ws/v1/meetings") {
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");

  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      if (typeof event.data !== "string") return;
      let envelope: unknown;
      try {
        envelope = JSON.parse(event.data);
      } catch {
        return;
      }

      if (!isMeetingEventEnvelope(envelope)) return;

      const store = useMeetingStore.getState();
      const { type, meeting_id, payload } = envelope;
      // 首次连接的 snapshot 是服务端当前会议的权威基线，允许它切换 active；
      // snapshot 触发的异步 baseline 会在 store 内再次校验 meetingId。
      const isSnapshotForCurrentView = type === "meeting_snapshot";
      if (
        !isSnapshotForCurrentView &&
        !isMeetingEventRelevant(type as MeetingEventType, meeting_id, store)
      ) {
        return;
      }

      switch (type as MeetingEventType) {
        case "meeting_snapshot": {
          const p = payload as MeetingSnapshotPayload;
          store.applySnapshot(p);
          if (meeting_id && meeting_id.trim() !== "" && meeting_id !== "null") {
            void store.syncBaselineTranscript(meeting_id);
          }
          void store.fetchHistory();
          break;
        }

        case "meeting_state_changed": {
          const p = payload as MeetingStateChangedPayload;
          store.updateMeetingState(
            p.status,
            p.started_at,
            p.ended_at,
            p.interruption_reason,
            meeting_id,
          );
          void store.fetchHistory();
          break;
        }

        case "transcript_partial": {
          const p = payload as TranscriptPartialPayload;
          store.setPartial(p.text, p.speaker_name || null, meeting_id);
          break;
        }

        case "transcript_reconciled": {
          const p = payload as TranscriptReconciledPayload;
          // 检测 revision 是否跳变，若跳变过大触发 resync
          if (
            meeting_id &&
            meeting_id.trim() !== "" &&
            p.transcript_revision > store.transcriptRevision + 10
          ) {
            void store.syncBaselineTranscript(meeting_id);
          } else {
            store.reconcileTranscript(
              p.replace_from_ms,
              p.segments,
              p.transcript_revision,
              p.content_revision,
              meeting_id,
            );
          }
          break;
        }

        case "speaker_updated": {
          const p = payload as SpeakerUpdatedPayload;
          store.setSpeaker(p.speaker_key, p.display_name, p.content_revision, meeting_id);
          break;
        }

        case "minutes_state_changed": {
          const p = payload as MinutesStateChangedPayload;
          store.setMinutesState(
            p.version,
            p.status,
            p.error_code,
            p.error_message,
            p.minutes,
            meeting_id,
            p.minutes_id,
          );
          void store.fetchHistory();
          break;
        }

        case "health_changed": {
          const p = payload as HealthChangedPayload;
          store.updateHealth({
            storage: p.storage,
            transcription: p.transcription,
            mic_muted: p.mic_muted,
            recovery_journal_active: p.recovery_journal_active ?? false,
          }, meeting_id);
          break;
        }

        case "transcription_gap": {
          const p = payload as TranscriptionGapPayload;
          store.addGap(p.start_ms, p.end_ms, p.reason, meeting_id);
          break;
        }

        case "resync_required": {
          if (meeting_id && meeting_id.trim() !== "") {
            void store.syncBaselineTranscript(meeting_id);
          }
          void store.fetchHistory();
          break;
        }
      }
    };

    const socket = new ReconnectingSocket(url, {
      onState: setConnectionState,
      onMessage: handleMessage,
    });
    socket.start();

    return () => {
      socket.stop();
    };
  }, [url]);

  return {
    connectionState,
    connected: connectionState === "open",
  };
}
