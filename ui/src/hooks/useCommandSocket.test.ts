import { describe, expect, it, vi } from "vitest";

import { CommandChannel } from "./useCommandSocket";

const SNAPSHOT = {
  pipeline: "running",
  subtitle: "connected",
  mic_muted: false,
  persona: "简练回答",
  voice: "warm",
  duplex_mode: "speaker_focus" as const,
  session_started_at: "2026-08-21T10:00:00+08:00",
};

class OpenSocket {
  static readonly OPEN = 1;
  readonly readyState = OpenSocket.OPEN;
  readonly sent: string[] = [];
  send(value: string) {
    this.sent.push(value);
  }
}

describe("CommandChannel", () => {
  it("waits for handshake then correlates an acknowledgement", async () => {
    const socket = new OpenSocket();
    const applyState = vi.fn();
    const channel = new CommandChannel({ applyState, timeoutMs: 1000 });
    channel.attach(socket as unknown as WebSocket);
    channel.receive(JSON.stringify({ event: "state", state: SNAPSHOT }));

    const pending = channel.send({ cmd: "set_mic_muted", muted: true });
    const request = JSON.parse(socket.sent[0] ?? "{}") as { request_id: string };
    channel.receive(JSON.stringify({
      request_id: request.request_id,
      cmd: "set_mic_muted",
      ok: true,
      state: { ...SNAPSHOT, mic_muted: true },
      error_code: null,
      message: null,
    }));

    await expect(pending).resolves.toMatchObject({ mic_muted: true });
    expect(applyState).toHaveBeenLastCalledWith(expect.objectContaining({ mic_muted: true }));
  });

  it("rejects a stable server error without applying speculative state", async () => {
    const socket = new OpenSocket();
    const applyState = vi.fn();
    const channel = new CommandChannel({ applyState, timeoutMs: 1000 });
    channel.attach(socket as unknown as WebSocket);
    channel.receive(JSON.stringify({ event: "state", state: SNAPSHOT }));

    const pending = channel.send({ cmd: "set_voice", voice: "bright" });
    const request = JSON.parse(socket.sent[0] ?? "{}") as { request_id: string };
    channel.receive(JSON.stringify({
      request_id: request.request_id,
      cmd: "set_voice",
      ok: false,
      state: SNAPSHOT,
      error_code: "command_failed",
      message: "音色不可用",
    }));

    await expect(pending).rejects.toThrow("音色不可用");
    expect(applyState).toHaveBeenLastCalledWith(expect.objectContaining({ voice: "warm" }));
  });
});
