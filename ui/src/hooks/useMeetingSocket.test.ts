import { describe, it, expect } from "vitest";
import { isMeetingEventEnvelope, isMeetingEventRelevant } from "./useMeetingSocket";

describe("useMeetingSocket", () => {
  it("validates MeetingEventEnvelope format", () => {
    const validEnvelope = {
      contract_version: "1",
      type: "transcript_reconciled",
      event_id: "evt-123",
      meeting_id: "m-456",
      occurred_at: "2026-08-21T10:00:00Z",
      payload: {
        transcript_revision: 2,
        content_revision: 2,
        replace_from_ms: 0,
        segments: [],
      },
    };

    expect(isMeetingEventEnvelope(validEnvelope)).toBe(true);

    // Invalid version
    expect(isMeetingEventEnvelope({ ...validEnvelope, contract_version: "2" })).toBe(false);

    // Missing payload
    expect(isMeetingEventEnvelope({ contract_version: "1", type: "event" })).toBe(false);

    // Non object
    expect(isMeetingEventEnvelope(null)).toBe(false);
    expect(isMeetingEventEnvelope("string")).toBe(false);
  });

  it("rejects events from another meeting while preserving selected minutes updates", () => {
    const state = {
      activeMeetingId: "m-current",
      selectedMeetingId: "m-history",
      selectedMeeting: { id: "m-history" },
    };

    expect(isMeetingEventRelevant("transcript_partial", "m-old", state)).toBe(false);
    expect(isMeetingEventRelevant("transcription_gap", "m-current", state)).toBe(true);
    expect(isMeetingEventRelevant("minutes_state_changed", "m-history", state)).toBe(true);
    expect(isMeetingEventRelevant("minutes_state_changed", "m-old", state)).toBe(false);
  });
});
