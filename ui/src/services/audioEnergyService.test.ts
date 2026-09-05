import { afterEach, describe, expect, it, vi } from "vitest";

import type { RuntimeStateSnapshot } from "../protocol";
import { AudioEnergyService } from "./audioEnergyService";

const STATE: RuntimeStateSnapshot = {
  mode: "meeting",
  pcm_owner: "meeting",
  pipeline: "stopped",
  subtitle: "connected",
  mic_muted: false,
  runtime_revision: 1,
  audio_levels: {
    microphone: 0.25,
    physical_output: 0.5,
    mixed: 0.625,
    updated_at_ns: 10,
  },
};

describe("AudioEnergyService", () => {
  it("shows output energy even when the microphone is muted, and clears it after failure", () => {
    const service = new AudioEnergyService();
    const outputState: RuntimeStateSnapshot = {
      ...STATE, mode: "subtitles", pcm_owner: "subtitles", mic_muted: true,
      subtitle_capture: { source: "physical_output", device_ref: "vrdev1_" + "A".repeat(43) },
      output_capture_active: true,
    };
    service.updateFromRuntimeState(outputState);
    expect(service.getEnergy()).toBe(0.5);
    service.updateFromRuntimeState({ ...outputState, output_capture_active: false });
    expect(service.getEnergy()).toBe(0);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("publishes the server mixed level without opening browser audio", () => {
    const getUserMedia = vi.fn();
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia } });
    const service = new AudioEnergyService();
    const subscriber = vi.fn();
    const unsubscribe = service.subscribe(subscriber);

    service.updateFromRuntimeState(STATE);

    expect(subscriber).toHaveBeenLastCalledWith(0.625);
    expect(service.getEnergy()).toBe(0.625);
    expect(getUserMedia).not.toHaveBeenCalled();
    unsubscribe();
  });

  it("publishes zero while muted", () => {
    const service = new AudioEnergyService();
    const subscriber = vi.fn();
    service.subscribe(subscriber);

    service.setMuted(true);
    service.updateFromRuntimeState(STATE);

    expect(service.getEnergy()).toBe(0);
    expect(subscriber).toHaveBeenLastCalledWith(0);
  });
});
