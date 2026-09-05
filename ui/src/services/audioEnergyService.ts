import type { RuntimeStateSnapshot } from "../protocol";

type EnergySubscriber = (energy: number) => void;

/** 将服务端实际处理的 PCM 能量快照分发给波形组件。 */
export class AudioEnergyService {
  private readonly subscribers = new Set<EnergySubscriber>();
  private currentEnergy = 0;
  private muted = false;

  public subscribe(callback: EnergySubscriber): () => void {
    this.subscribers.add(callback);
    callback(this.currentEnergy);
    return () => {
      this.subscribers.delete(callback);
    };
  }

  public setMuted(muted: boolean): void {
    this.muted = muted;
    if (muted) this.publish(0);
  }

  public getEnergy(): number {
    return this.currentEnergy;
  }

  public updateFromRuntimeState(state: RuntimeStateSnapshot): void {
    const levels = state.audio_levels;
    const outputOnly = state.mode === "subtitles" && state.subtitle_capture?.source === "physical_output";
    this.publish(this.muted ? 0 : outputOnly
      ? (state.output_capture_active ? (levels?.physical_output ?? 0) : 0)
      : state.mic_muted ? 0 : (levels?.mixed ?? 0));
  }

  private publish(value: number): void {
    this.currentEnergy = Number.isFinite(value)
      ? Math.min(1, Math.max(0, value))
      : 0;
    this.subscribers.forEach((callback) => callback(this.currentEnergy));
  }
}

export const audioEnergyService = new AudioEnergyService();
