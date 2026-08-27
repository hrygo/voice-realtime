/**
 * Local privacy-preserving aggregate metrics for Inner OS.
 * Strictly zero content, zero prompts, zero transcripts.
 */

export interface InnerOSAggregateMetrics {
  totalQueriesSubmitted: number;
  totalQueriesAccepted: number;
  totalQueriesCompleted: number;
  totalQueriesFailed: number;
  totalQueriesCancelled: number;
  totalDraftsCopied: number;
  totalExchangesSaved: number;
  latencyBuckets: {
    under2s: number;
    under5s: number;
    over5s: number;
  };
}

let metrics: InnerOSAggregateMetrics = {
  totalQueriesSubmitted: 0,
  totalQueriesAccepted: 0,
  totalQueriesCompleted: 0,
  totalQueriesFailed: 0,
  totalQueriesCancelled: 0,
  totalDraftsCopied: 0,
  totalExchangesSaved: 0,
  latencyBuckets: {
    under2s: 0,
    under5s: 0,
    over5s: 0,
  },
};

export const innerOSMetrics = {
  recordQuerySubmitted() {
    metrics.totalQueriesSubmitted += 1;
  },

  recordQueryAccepted() {
    metrics.totalQueriesAccepted += 1;
  },

  recordQueryCompleted(elapsedMs: number) {
    metrics.totalQueriesCompleted += 1;
    if (elapsedMs < 2000) {
      metrics.latencyBuckets.under2s += 1;
    } else if (elapsedMs < 5000) {
      metrics.latencyBuckets.under5s += 1;
    } else {
      metrics.latencyBuckets.over5s += 1;
    }
  },

  recordQueryFailed() {
    metrics.totalQueriesFailed += 1;
  },

  recordQueryCancelled() {
    metrics.totalQueriesCancelled += 1;
  },

  recordDraftCopied() {
    metrics.totalDraftsCopied += 1;
  },

  recordExchangeSaved() {
    metrics.totalExchangesSaved += 1;
  },

  getSnapshot(): Readonly<InnerOSAggregateMetrics> {
    return {
      ...metrics,
      latencyBuckets: { ...metrics.latencyBuckets },
    };
  },

  reset() {
    metrics = {
      totalQueriesSubmitted: 0,
      totalQueriesAccepted: 0,
      totalQueriesCompleted: 0,
      totalQueriesFailed: 0,
      totalQueriesCancelled: 0,
      totalDraftsCopied: 0,
      totalExchangesSaved: 0,
      latencyBuckets: {
        under2s: 0,
        under5s: 0,
        over5s: 0,
      },
    };
  },
};
