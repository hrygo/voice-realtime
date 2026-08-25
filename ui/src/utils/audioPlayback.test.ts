import { afterEach, describe, expect, it, vi } from "vitest";

import { playAudioBlob } from "./audioPlayback";

class FakeAudio {
  static rejectPlay = false;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;

  play(): Promise<void> {
    if (FakeAudio.rejectPlay) return Promise.reject(new Error("blocked"));
    queueMicrotask(() => this.onended?.());
    return Promise.resolve();
  }
}

describe("playAudioBlob", () => {
  afterEach(() => {
    FakeAudio.rejectPlay = false;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("revokes the object URL after playback ends", async () => {
    const revoke = vi.fn();
    vi.stubGlobal("Audio", FakeAudio);
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:preview"),
      revokeObjectURL: revoke,
    });

    await playAudioBlob(new Blob(["audio"]));

    expect(revoke).toHaveBeenCalledOnce();
    expect(revoke).toHaveBeenCalledWith("blob:preview");
  });

  it("revokes the object URL when play is rejected", async () => {
    FakeAudio.rejectPlay = true;
    const revoke = vi.fn();
    vi.stubGlobal("Audio", FakeAudio);
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:blocked"),
      revokeObjectURL: revoke,
    });

    await expect(playAudioBlob(new Blob(["audio"]))).rejects.toThrow("blocked");
    expect(revoke).toHaveBeenCalledOnce();
    expect(revoke).toHaveBeenCalledWith("blob:blocked");
  });
});
