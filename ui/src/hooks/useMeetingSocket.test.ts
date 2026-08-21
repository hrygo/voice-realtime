import { describe, it, expect } from "vitest";
import { isMeetingEventEnvelope } from "./useMeetingSocket";

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
});
