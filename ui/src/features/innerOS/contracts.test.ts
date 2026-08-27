import { describe, it, expect } from "vitest";
import {
  isInnerOSAnswer,
  isInnerOSEventEnvelope,
  type InnerOSAnswer,
} from "./contracts";

describe("Inner OS Contracts & Validation", () => {
  it("validates valid InnerOSAnswer object", () => {
    const answer: InnerOSAnswer = {
      intent: "mixed",
      evidence: [
        {
          segment_id: "00000000-0000-0000-0000-000000000001",
          start_ms: 1000,
          end_ms: 5000,
          speaker_key: "speaker-1",
          speaker_name: "张总",
          text: "延迟必须小于15ms",
          content_hash: "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
      ],
      facts: [
        {
          text: "延迟必须小于15ms",
          evidence_segment_ids: ["00000000-0000-0000-0000-000000000001"],
        },
      ],
      judgements: [
        {
          text: "排期较紧凑",
          basis_segment_ids: ["00000000-0000-0000-0000-000000000001"],
          uncertainty: "medium",
          uncertainty_reason: "尚未明确验收时间",
        },
      ],
      draft: {
        text: "已确认指标小于15ms",
      },
      limitations: [],
    };

    expect(isInnerOSAnswer(answer)).toBe(true);
  });

  it("rejects invalid answer structure", () => {
    expect(isInnerOSAnswer(null)).toBe(false);
    expect(isInnerOSAnswer({})).toBe(false);
    expect(isInnerOSAnswer({ intent: "invalid" })).toBe(false);
    expect(isInnerOSAnswer({ intent: "fact", evidence: "not-array" })).toBe(false);
  });

  it("validates event envelope correctly", () => {
    const event = {
      contract_version: "1",
      type: "inner_os_answer_completed",
      event_id: "e1",
      meeting_id: "m1",
      query_id: "q1",
      occurred_at: "2026-08-27T00:00:00Z",
      payload: {},
    };

    expect(isInnerOSEventEnvelope(event)).toBe(true);
    expect(isInnerOSEventEnvelope({ ...event, contract_version: "2" })).toBe(false);
    expect(isInnerOSEventEnvelope({ ...event, type: "other_event" })).toBe(false);
  });
});
