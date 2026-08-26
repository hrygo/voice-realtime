import { describe, expect, it } from "vitest";

import { buildApiUrl, deriveWebSocketUrl, normalizeBaseUrl } from "./runtimeConfig";

describe("runtimeConfig endpoint helpers", () => {
  it("normalizes a separately deployed API base URL", () => {
    expect(normalizeBaseUrl(" https://meeting.example.test/// ")).toBe(
      "https://meeting.example.test",
    );
    expect(buildApiUrl("https://meeting.example.test/", "/api/v1/runtime")).toBe(
      "https://meeting.example.test/api/v1/runtime",
    );
  });

  it("derives a WebSocket endpoint from the API origin when explicitly configured", () => {
    expect(deriveWebSocketUrl("https://meeting.example.test/base", "/ws/v1/control")).toBe(
      "wss://meeting.example.test/ws/v1/control",
    );
    expect(deriveWebSocketUrl("", "/ws/v1/meetings")).toBe("/ws/v1/meetings");
  });
});
