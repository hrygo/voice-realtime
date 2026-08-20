import { afterEach, describe, expect, it, vi } from "vitest";

import { ReconnectingSocket } from "./useEventSocket";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static readonly OPEN = 1;

  readonly readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    MockWebSocket.instances.push(this);
  }

  close() {
    this.onclose?.();
  }
}

describe("ReconnectingSocket", () => {
  afterEach(() => {
    vi.useRealTimers();
    MockWebSocket.instances = [];
  });

  it("does not reconnect after disposal", () => {
    vi.useFakeTimers();
    const socket = new ReconnectingSocket("/ws/events", {
      createSocket: (url) => new MockWebSocket(url) as unknown as WebSocket,
    });

    socket.start();
    socket.stop();
    vi.advanceTimersByTime(60_000);

    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it("reconnects with bounded exponential delays", () => {
    vi.useFakeTimers();
    const states: string[] = [];
    const socket = new ReconnectingSocket("/ws/events", {
      createSocket: (url) => new MockWebSocket(url) as unknown as WebSocket,
      onState: (state) => states.push(state),
    });

    socket.start();
    MockWebSocket.instances[0]?.onclose?.();
    vi.advanceTimersByTime(999);
    expect(MockWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(MockWebSocket.instances).toHaveLength(2);
    expect(states).toContain("closed");
  });
});
