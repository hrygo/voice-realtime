import { useEffect, useRef, useState } from "react";
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

export function useMeetingSocket(url = "/ws/v1/meetings") {
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const meetingStore = useMeetingStore();
  const meetingStoreRef = useRef(meetingStore);
  meetingStoreRef.current = meetingStore;

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

      const store = meetingStoreRef.current;
      const { type, meeting_id, payload } = envelope;

      switch (type as MeetingEventType) {
        case "meeting_snapshot": {
          const p = payload as MeetingSnapshotPayload;
          store.applySnapshot(p);
          if (meeting_id) {
            void store.syncBaselineTranscript(meeting_id);
          }
          break;
        }

        case "meeting_state_changed": {
          const p = payload as MeetingStateChangedPayload;
          store.updateMeetingState(p.status, p.started_at, p.ended_at, p.interruption_reason);
          break;
        }

        case "transcript_partial": {
          const p = payload as TranscriptPartialPayload;
          store.setPartial(p.text, p.speaker_name || null);
          break;
        }

        case "transcript_reconciled": {
          const p = payload as TranscriptReconciledPayload;
          // 检测 revision 是否跳变，若跳变过大触发 resync
          if (p.transcript_revision > store.transcriptRevision + 10) {
            void store.syncBaselineTranscript(meeting_id);
          } else {
            store.reconcileTranscript(
              p.replace_from_ms,
              p.segments,
              p.transcript_revision,
              p.content_revision,
            );
          }
          break;
        }

        case "speaker_updated": {
          const p = payload as SpeakerUpdatedPayload;
          store.setSpeaker(p.speaker_key, p.display_name, p.content_revision);
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
          );
          break;
        }

        case "health_changed": {
          const p = payload as HealthChangedPayload;
          store.updateHealth({
            storage: p.storage,
            transcription: p.transcription,
            mic_muted: p.mic_muted,
            recovery_journal_active: p.recovery_journal_active ?? false,
          });
          break;
        }

        case "transcription_gap": {
          const p = payload as TranscriptionGapPayload;
          store.addGap(p.start_ms, p.end_ms, p.reason);
          break;
        }

        case "resync_required": {
          if (meeting_id) {
            void store.syncBaselineTranscript(meeting_id);
          }
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
