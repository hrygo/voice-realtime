import { describe, expect, it } from "vitest";

import { sessionElapsedSeconds } from "./StatusBar";

describe("sessionElapsedSeconds", () => {
  it("derives elapsed time from the authoritative server timestamp", () => {
    expect(sessionElapsedSeconds("2026-08-21T00:00:00.000Z", Date.parse("2026-08-21T00:01:05.900Z")))
      .toBe(65);
  });

  it("resets when the session is stopped or the timestamp is invalid", () => {
    expect(sessionElapsedSeconds(null)).toBe(0);
    expect(sessionElapsedSeconds("invalid")).toBe(0);
  });
});
