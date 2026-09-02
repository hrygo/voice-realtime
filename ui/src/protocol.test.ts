import { describe, expect, it } from "vitest";

import { isServicesResponse } from "./protocol";

describe("isServicesResponse", () => {
  it("accepts the additive health and workload response", () => {
    expect(isServicesResponse({
      network_scope: "local",
      services: [{
        name: "speechrail",
        status: "ok",
        url: "http://127.0.0.1:8201/health",
        workload: "ready",
        ws_state: "connected",
        reconnect_count: 2,
        last_event_age_ms: 120,
        dropped_chunks: 0,
        gap_count: 0,
      }],
      diagnostics: { subtitles: {} },
    })).toBe(true);
  });

  it("rejects malformed service status fields at the API boundary", () => {
    expect(isServicesResponse({
      services: [{
        name: "speechrail",
        status: "future-status",
        url: "http://127.0.0.1:8201/health",
      }],
    })).toBe(false);

    expect(isServicesResponse({
      services: [{
        name: "lm",
        status: "ok",
        url: "http://127.0.0.1:1234/v1/models",
        model_present: "yes",
      }],
    })).toBe(false);
  });
});
