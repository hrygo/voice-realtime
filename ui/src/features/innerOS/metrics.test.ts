import { describe, it, expect, beforeEach } from "vitest";
import { innerOSMetrics } from "./metrics";

describe("Inner OS Aggregate Metrics", () => {
  beforeEach(() => {
    innerOSMetrics.reset();
  });

  it("increments counters and bucketizes latencies without content", () => {
    innerOSMetrics.recordQuerySubmitted();
    innerOSMetrics.recordQueryAccepted();
    innerOSMetrics.recordQueryCompleted(1200);
    innerOSMetrics.recordQueryCompleted(3500);
    innerOSMetrics.recordDraftCopied();
    innerOSMetrics.recordExchangeSaved();

    const snapshot = innerOSMetrics.getSnapshot();
    expect(snapshot.totalQueriesSubmitted).toBe(1);
    expect(snapshot.totalQueriesAccepted).toBe(1);
    expect(snapshot.totalQueriesCompleted).toBe(2);
    expect(snapshot.latencyBuckets.under2s).toBe(1);
    expect(snapshot.latencyBuckets.under5s).toBe(1);
    expect(snapshot.totalDraftsCopied).toBe(1);
    expect(snapshot.totalExchangesSaved).toBe(1);
  });
});
