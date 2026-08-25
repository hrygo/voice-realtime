import { afterEach, describe, expect, it, vi } from "vitest";

import type { PCMOwner } from "../contracts/meetingContract";
import { isRuntimeState, type RuntimeStateSnapshot } from "../protocol";
import { CommandChannel } from "./useCommandSocket";

const SNAPSHOT: RuntimeStateSnapshot = {
  mode: "assistant",
  pcm_owner: "assistant",
  runtime_revision: 1,
  active_meeting_id: null,
  meeting_state: null,
  meeting_started_at: null,
  pipeline: "running",
  subtitle: "connected",
  mic_muted: false,
  persona: "简练回答",
  voice: "warm",
  duplex_mode: "speaker_focus",
  session_started_at: "2026-08-21T10:00:00+08:00",
};

function snapshot(overrides: Partial<RuntimeStateSnapshot> = {}): RuntimeStateSnapshot {
  return { ...SNAPSHOT, ...overrides };
}

function runtimeEvent(state: RuntimeStateSnapshot): string {
  return JSON.stringify({ contract_version: "1", event: "runtime_state", state });
}

class OpenSocket {
  static readonly OPEN = 1;
  readonly readyState = OpenSocket.OPEN;
  readonly sent: string[] = [];

  send(value: string): void {
    this.sent.push(value);
  }
}

describe("runtime state protocol", () => {
  it("accepts subtitles mode and PCM ownership", () => {
    const owner: PCMOwner = "subtitles";

    expect(isRuntimeState(snapshot({ mode: "subtitles", pcm_owner: owner }))).toBe(true);
    expect(isRuntimeState(snapshot({ pcm_owner: "invalid" as PCMOwner }))).toBe(false);
  });

  it.each(["mode", "pcm_owner", "runtime_revision"] as const)(
    "requires %s in a legal runtime snapshot",
    (key) => {
      const invalid: Record<string, unknown> = { ...SNAPSHOT };
      delete invalid[key];

      expect(isRuntimeState(invalid)).toBe(false);
    },
  );
});

describe("CommandChannel", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("waits for handshake then correlates an acknowledgement", async () => {
    const socket = new OpenSocket();
    const applyState = vi.fn();
    const channel = new CommandChannel({ applyState, timeoutMs: 1000 });
    channel.attach(socket as unknown as WebSocket);
    channel.receive(runtimeEvent(SNAPSHOT));

    const pending = channel.send({ cmd: "set_mic_muted", muted: true });
    const request = JSON.parse(socket.sent[0] ?? "{}") as { request_id: string };
    channel.receive(JSON.stringify({
      request_id: request.request_id,
      cmd: "set_mic_muted",
      ok: true,
      state: snapshot({ mic_muted: true }),
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
    channel.receive(runtimeEvent(SNAPSHOT));

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

  it.each(["broadcast-first", "ack-first"] as const)(
    "accepts runtime broadcast and command ack in %s order",
    async (order) => {
      const socket = new OpenSocket();
      const applyState = vi.fn();
      const channel = new CommandChannel({ applyState, timeoutMs: 1000 });
      channel.attach(socket as unknown as WebSocket);
      channel.receive(runtimeEvent(snapshot({ runtime_revision: 7 })));

      const pending = channel.send({ cmd: "start_subtitles" });
      const request = JSON.parse(socket.sent[0] ?? "{}") as { request_id: string };
      const next = snapshot({
        mode: "subtitles",
        pcm_owner: "subtitles",
        runtime_revision: 8,
      });
      const ack = JSON.stringify({
        contract_version: "1",
        request_id: request.request_id,
        cmd: "start_subtitles",
        ok: true,
        state: next,
      });

      if (order === "broadcast-first") {
        channel.receive(runtimeEvent(next));
        channel.receive(ack);
      } else {
        channel.receive(ack);
        channel.receive(runtimeEvent(next));
      }

      await expect(pending).resolves.toMatchObject({
        mode: "subtitles",
        pcm_owner: "subtitles",
        runtime_revision: 8,
      });
      expect(channel.latestState).toMatchObject({ mode: "subtitles", runtime_revision: 8 });
      expect(channel.highestRuntimeRevision).toBe(8);
    },
  );

  it("resets the revision baseline when a new socket takes ownership", () => {
    const applyState = vi.fn();
    const channel = new CommandChannel({ applyState });
    channel.attach(new OpenSocket() as unknown as WebSocket);
    channel.receive(runtimeEvent(snapshot({
      mode: "subtitles",
      pcm_owner: "subtitles",
      runtime_revision: 9,
    })));

    channel.attach(new OpenSocket() as unknown as WebSocket);
    channel.receive(runtimeEvent(snapshot({
      mode: "assistant",
      pcm_owner: "assistant",
      runtime_revision: 0,
    })));

    expect(channel.latestState).toMatchObject({
      mode: "assistant",
      pcm_owner: "assistant",
      runtime_revision: 0,
    });
    expect(channel.highestRuntimeRevision).toBe(0);
    expect(applyState).toHaveBeenLastCalledWith(expect.objectContaining({
      mode: "assistant",
      pcm_owner: "assistant",
      runtime_revision: 0,
    }));
  });

  it("keeps ownership from the highest revision while accepting fresh UI fields", async () => {
    const socket = new OpenSocket();
    const applyState = vi.fn();
    const channel = new CommandChannel({ applyState, timeoutMs: 1000 });
    channel.attach(socket as unknown as WebSocket);
    channel.receive(runtimeEvent(snapshot({ runtime_revision: 7 })));

    const pending = channel.send({ cmd: "set_mic_muted", muted: true });
    const request = JSON.parse(socket.sent[0] ?? "{}") as { request_id: string };
    channel.receive(runtimeEvent(snapshot({
      mode: "subtitles",
      pcm_owner: "subtitles",
      runtime_revision: 9,
    })));
    channel.receive(JSON.stringify({
      request_id: request.request_id,
      cmd: "set_mic_muted",
      ok: true,
      state: snapshot({
        mode: "assistant",
        pcm_owner: "assistant",
        runtime_revision: 8,
        mic_muted: true,
      }),
    }));

    await expect(pending).resolves.toMatchObject({
      mode: "subtitles",
      pcm_owner: "subtitles",
      runtime_revision: 9,
      mic_muted: true,
    });
    expect(applyState).toHaveBeenLastCalledWith(expect.objectContaining({
      mode: "subtitles",
      pcm_owner: "subtitles",
      runtime_revision: 9,
      mic_muted: true,
    }));
  });

  it("reports same-revision ownership conflicts without overwriting ownership", () => {
    const applyState = vi.fn();
    const onProtocolError = vi.fn();
    const channel = new CommandChannel({ applyState, onProtocolError });
    channel.receive(runtimeEvent(snapshot({
      mode: "meeting",
      pcm_owner: "meeting",
      active_meeting_id: "meeting-current",
      meeting_state: "recording",
      meeting_started_at: "2026-08-25T20:00:00+08:00",
      runtime_revision: 9,
    })));

    channel.receive(runtimeEvent(snapshot({
      mode: "meeting",
      pcm_owner: "meeting",
      active_meeting_id: "meeting-conflict",
      meeting_state: "finalizing",
      meeting_started_at: "2026-08-25T20:01:00+08:00",
      runtime_revision: 9,
      persona: "最新 persona",
      mic_muted: true,
    })));

    expect(onProtocolError).toHaveBeenCalledOnce();
    expect(onProtocolError).toHaveBeenCalledWith(expect.objectContaining({ code: "protocol_error" }));
    expect(channel.latestState).toMatchObject({
      mode: "meeting",
      pcm_owner: "meeting",
      active_meeting_id: "meeting-current",
      meeting_state: "recording",
      meeting_started_at: "2026-08-25T20:00:00+08:00",
      runtime_revision: 9,
      persona: "最新 persona",
      mic_muted: true,
    });
  });

  it("adds contract_version to start_subtitles", async () => {
    const socket = new OpenSocket();
    const channel = new CommandChannel({ applyState: vi.fn(), timeoutMs: 1000 });
    channel.attach(socket as unknown as WebSocket);
    channel.receive(runtimeEvent(SNAPSHOT));

    const pending = channel.send({ cmd: "start_subtitles" });

    expect(JSON.parse(socket.sent[0] ?? "{}")).toMatchObject({
      cmd: "start_subtitles",
      contract_version: "1",
    });
    channel.dispose();
    await expect(pending).rejects.toMatchObject({ code: "service_unavailable" });
  });

  it("does not become ready after an invalid first snapshot", async () => {
    const socket = new OpenSocket();
    const onReady = vi.fn();
    const channel = new CommandChannel({ applyState: vi.fn(), onReady });
    channel.attach(socket as unknown as WebSocket);
    channel.receive(JSON.stringify({
      event: "runtime_state",
      state: {
        pipeline: "running",
        subtitle: "connected",
        mic_muted: false,
      },
    }));

    await expect(channel.send({ cmd: "set_mic_muted", muted: true })).rejects.toMatchObject({
      code: "service_unavailable",
    });
    expect(onReady).not.toHaveBeenCalledWith(true);
    expect(socket.sent).toEqual([]);
  });

  it("reconciles a higher HTTP snapshot through the channel merge and clears reconciling", async () => {
    let resolveFetch: ((value: object) => void) | undefined;
    const fetchMock = vi.fn(() => new Promise<object>((resolve) => {
      resolveFetch = resolve;
    }));
    vi.stubGlobal("fetch", fetchMock);
    const applyState = vi.fn();
    const channel = new CommandChannel({ applyState });
    channel.receive(runtimeEvent(SNAPSHOT));

    const pending = channel.reconcileRuntime();
    expect(channel.reconciling).toBe(true);
    resolveFetch?.({
      ok: true,
      json: async () => snapshot({
        mode: "subtitles",
        pcm_owner: "subtitles",
        runtime_revision: 2,
      }),
    });

    await expect(pending).resolves.toMatchObject({ mode: "subtitles", runtime_revision: 2 });
    expect(fetchMock).toHaveBeenCalledWith("/api/runtime");
    expect(applyState).toHaveBeenLastCalledWith(expect.objectContaining({
      mode: "subtitles",
      runtime_revision: 2,
    }));
    expect(channel.reconciling).toBe(false);
  });

  it("does not let a lower HTTP revision overwrite ownership", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => snapshot({
        mode: "assistant",
        pcm_owner: "assistant",
        runtime_revision: 8,
        mic_muted: true,
      }),
    }));
    const channel = new CommandChannel({ applyState: vi.fn() });
    channel.receive(runtimeEvent(snapshot({
      mode: "subtitles",
      pcm_owner: "subtitles",
      runtime_revision: 9,
    })));

    await expect(channel.reconcileRuntime()).resolves.toMatchObject({
      mode: "subtitles",
      pcm_owner: "subtitles",
      runtime_revision: 9,
      mic_muted: true,
    });
  });

  it("returns service_unavailable on HTTP failure without sending a reverse command", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503 }));
    const socket = new OpenSocket();
    const channel = new CommandChannel({ applyState: vi.fn() });
    channel.attach(socket as unknown as WebSocket);
    channel.receive(runtimeEvent(SNAPSHOT));

    await expect(channel.reconcileRuntime()).rejects.toMatchObject({ code: "service_unavailable" });
    expect(socket.sent).toEqual([]);
    expect(channel.reconciling).toBe(false);
  });
});
