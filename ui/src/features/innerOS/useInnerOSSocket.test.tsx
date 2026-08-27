import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { useInnerOSSocket } from "./useInnerOSSocket";
import { useInnerOSStore } from "./innerOSStore";
import React from "react";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;
  readyState = MockWebSocket.OPEN;
  url: string;
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  sentMessages: string[] = [];

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
    setTimeout(() => {
      if (this.onopen) this.onopen({});
    }, 0);
  }

  send(data: string) {
    this.sentMessages.push(data);
  }

  close() {
    this.readyState = 3;
    if (this.onclose) this.onclose({});
  }
}

interface TestHarnessProps {
  meetingId: string | null;
  enabled: boolean;
  onHookResult: (res: ReturnType<typeof useInnerOSSocket>) => void;
}

const TestHarness: React.FC<TestHarnessProps> = ({ meetingId, enabled, onHookResult }) => {
  const res = useInnerOSSocket({ meetingId, enabled });
  React.useEffect(() => {
    onHookResult(res);
  });
  return null;
};

describe("useInnerOSSocket", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    useInnerOSStore.getState().reset();

    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.restoreAllMocks();
  });

  it("connects to private inner-os WS and handles query submission & completion", async () => {
    let hookRes: ReturnType<typeof useInnerOSSocket> | null = null;

    act(() => {
      root.render(
        <TestHarness
          meetingId="00000000-0000-0000-0000-000000000001"
          enabled={true}
          onHookResult={(r) => {
            hookRes = r;
          }}
        />,
      );
    });

    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });

    expect(MockWebSocket.instances.length).toBe(1);
    expect(MockWebSocket.instances[0].url).toContain(
      "/ws/v1/meetings/00000000-0000-0000-0000-000000000001/inner-os",
    );

    let queryId: string | null = null;
    act(() => {
      queryId = hookRes?.sendQuery("有什么结论？", "mixed", 1, null, []) ?? null;
    });

    expect(queryId).toBeTruthy();
    expect(MockWebSocket.instances[0].sentMessages.length).toBe(1);

    const sentCmd = JSON.parse(MockWebSocket.instances[0].sentMessages[0]);
    expect(sentCmd.cmd).toBe("query");
    expect(sentCmd.question).toBe("有什么结论？");

    const mockCompletedEnvelope = {
      contract_version: "1",
      type: "inner_os_answer_completed",
      event_id: "e-1",
      meeting_id: "00000000-0000-0000-0000-000000000001",
      query_id: queryId,
      occurred_at: "2026-08-27T00:00:00Z",
      payload: {
        intent: "mixed",
        evidence: [],
        facts: [{ text: "事实1", evidence_segment_ids: [] }],
        judgements: [],
        draft: null,
        limitations: [],
      },
    };

    act(() => {
      MockWebSocket.instances[0].onmessage?.({
        data: JSON.stringify(mockCompletedEnvelope),
      });
    });

    expect(useInnerOSStore.getState().queryStatus).toBe("completed");
    expect(useInnerOSStore.getState().activeAnswer?.facts[0].text).toBe("事实1");
  });

  it("handles cancellation gracefully", async () => {
    let hookRes: ReturnType<typeof useInnerOSSocket> | null = null;

    act(() => {
      root.render(
        <TestHarness
          meetingId="00000000-0000-0000-0000-000000000001"
          enabled={true}
          onHookResult={(r) => {
            hookRes = r;
          }}
        />,
      );
    });

    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });

    act(() => {
      hookRes?.sendQuery("长耗时问题", "analysis", 1);
    });

    act(() => {
      hookRes?.sendCancel();
    });

    expect(useInnerOSStore.getState().queryStatus).toBe("cancelled");
    expect(MockWebSocket.instances[0].sentMessages.length).toBe(2);
    const cancelCmd = JSON.parse(MockWebSocket.instances[0].sentMessages[1]);
    expect(cancelCmd.cmd).toBe("cancel");
  });
});
