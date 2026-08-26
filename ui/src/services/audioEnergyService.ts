/**
 * Web Audio API 麦克风实时声学能量分析服务 (单例模式)
 * 采用 60FPS 频域人声频带 (100Hz~3500Hz) + 时域峰峰值双通道分析 + 自动 AudioContext 唤醒，
 * 解决浏览器 AudioContext suspended 导致静音、以及普通说话 RMS 过小无法触发可视波动的问题。
 */

type EnergySubscriber = (energy: number) => void;

class AudioEnergyService {
  private audioContext: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private stream: MediaStream | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private timeData: Uint8Array<ArrayBuffer> | null = null;
  private freqData: Uint8Array<ArrayBuffer> | null = null;

  private subscribers = new Set<EnergySubscriber>();
  private animFrameId: number | null = null;
  private isRunning = false;
  private isMuted = false;

  // 物理平滑阻尼状态
  private currentEnergy = 0.08;
  private lastEnergy = 0.08;
  private boundResumeHandler: (() => void) | null = null;

  public subscribe(callback: EnergySubscriber): () => void {
    this.subscribers.add(callback);
    if (this.subscribers.size === 1) {
      void this.start();
    }
    return () => {
      this.subscribers.delete(callback);
      if (this.subscribers.size === 0) {
        this.stop();
      }
    };
  }

  public setMuted(muted: boolean): void {
    this.isMuted = muted;
  }

  public getEnergy(): number {
    return this.currentEnergy;
  }

  private async start(): Promise<void> {
    if (this.isRunning) return;
    this.isRunning = true;

    // 环境检查（SSR 或测试环境降级）
    if (typeof window === "undefined" || !navigator?.mediaDevices?.getUserMedia) {
      this.startFallbackLoop();
      return;
    }

    try {
      const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      if (!AudioCtx) {
        this.startFallbackLoop();
        return;
      }

      this.audioContext = new AudioCtx();

      // 绑定用户手势以确保 AudioContext 从 suspended 顺利唤醒
      this.setupAudioContextResume(this.audioContext);

      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });

      if (!this.isRunning) {
        // 如果在获取权限期间已卸载
        this.stream.getTracks().forEach((t) => t.stop());
        return;
      }

      this.analyser = this.audioContext.createAnalyser();
      this.analyser.fftSize = 256;
      this.analyser.smoothingTimeConstant = 0.3; // 提升实时爆发力

      this.source = this.audioContext.createMediaStreamSource(this.stream);
      this.source.connect(this.analyser);

      this.timeData = new Uint8Array(new ArrayBuffer(this.analyser.fftSize));
      this.freqData = new Uint8Array(new ArrayBuffer(this.analyser.frequencyBinCount));

      this.startLoop();
    } catch {
      // 麦克风权限未授予或被占用时平滑降级
      this.startFallbackLoop();
    }
  }


  private setupAudioContextResume(ctx: AudioContext): void {
    const tryResume = () => {
      if (ctx.state === "suspended") {
        void ctx.resume();
      }
    };

    tryResume();

    this.boundResumeHandler = tryResume;
    window.addEventListener("pointerdown", tryResume, { passive: true });
    window.addEventListener("keydown", tryResume, { passive: true });
    window.addEventListener("touchstart", tryResume, { passive: true });
  }

  private removeAudioContextResume(): void {
    if (this.boundResumeHandler) {
      window.removeEventListener("pointerdown", this.boundResumeHandler);
      window.removeEventListener("keydown", this.boundResumeHandler);
      window.removeEventListener("touchstart", this.boundResumeHandler);
      this.boundResumeHandler = null;
    }
  }

  private stop(): void {
    this.isRunning = false;
    this.removeAudioContextResume();

    if (this.animFrameId !== null) {
      cancelAnimationFrame(this.animFrameId);
      this.animFrameId = null;
    }

    if (this.stream) {
      this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
    }
    if (this.source) {
      this.source.disconnect();
      this.source = null;
    }
    if (this.audioContext) {
      this.audioContext.close().catch(() => {});
      this.audioContext = null;
    }
    this.analyser = null;
    this.timeData = null;
    this.freqData = null;
  }

  private startLoop(): void {
    let recentMin = 0.05;
    let recentMax = 0.15;

    const loop = () => {
      if (!this.isRunning) return;

      let targetEnergy = 0.08;

      if (this.isMuted) {
        targetEnergy = 0.02;
      } else if (this.analyser && this.timeData && this.freqData) {
        // 如果 context 被挂起则尝试唤醒
        if (this.audioContext?.state === "suspended") {
          void this.audioContext.resume();
        }

        // 1. 频域人声带分析（100Hz ~ 3500Hz，对应 bin 1 到 30）
        this.analyser.getByteFrequencyData(this.freqData);
        let voiceSum = 0;
        let voiceMax = 0;
        const voiceBins = Math.min(32, this.freqData.length);
        for (let i = 1; i < voiceBins; i++) {
          const val = this.freqData[i];
          voiceSum += val;
          if (val > voiceMax) voiceMax = val;
        }
        const voiceAvg = voiceSum / (voiceBins - 1);

        // 2. 时域峰峰值与 RMS 综合分析
        this.analyser.getByteTimeDomainData(this.timeData);
        let minSample = 255;
        let maxSample = 0;
        let sumSquares = 0;
        const len = this.timeData.length;
        for (let i = 0; i < len; i++) {
          const s = this.timeData[i];
          if (s < minSample) minSample = s;
          if (s > maxSample) maxSample = s;
          const norm = (s - 128) / 128;
          sumSquares += norm * norm;
        }
        const peakToPeak = (maxSample - minSample) / 256;
        const rms = Math.sqrt(sumSquares / len);

        // 3. 自适应动态增益 (AGC) 算法：动态追踪环境底噪，极度放大真实说话
        const rawEnergy = Math.max(voiceAvg / 45, peakToPeak * 4.0, rms * 8.0);

        recentMin += (rawEnergy - recentMin) * 0.005; // 缓慢追踪底噪
        recentMax += (rawEnergy - recentMax) * (rawEnergy > recentMax ? 0.2 : 0.01);
        if (recentMax < recentMin + 0.1) recentMax = recentMin + 0.1;

        // 归一化感知能量
        const normalized = Math.max(0, Math.min(1, (rawEnergy - recentMin) / (recentMax - recentMin)));

        if (normalized > 0.05) {
          // 真人开口说话：极大扩展动态响应范围 (0.35 ~ 1.25)
          const boosted = Math.pow(normalized, 0.65);
          targetEnergy = 0.15 + boosted * 0.95;
        } else {
          // 无人讲话待命：保持安静平稳基线 0.08
          targetEnergy = 0.08;
        }
      }

      // 4. 广播级 Attack (极速 0.65) / Decay (温润 0.15) 阻尼滤波
      const attackFactor = 0.65;
      const decayFactor = 0.15;
      const factor = targetEnergy > this.currentEnergy ? attackFactor : decayFactor;
      this.currentEnergy += (targetEnergy - this.currentEnergy) * factor;

      if (Math.abs(this.currentEnergy - this.lastEnergy) > 0.001) {
        this.lastEnergy = this.currentEnergy;
        this.subscribers.forEach((cb) => cb(this.currentEnergy));
      }

      this.animFrameId = requestAnimationFrame(loop);
    };

    this.animFrameId = requestAnimationFrame(loop);
  }


  private startFallbackLoop(): void {
    const loop = () => {
      if (!this.isRunning) return;
      this.currentEnergy = this.isMuted ? 0.02 : 0.08;
      this.subscribers.forEach((cb) => cb(this.currentEnergy));
      this.animFrameId = requestAnimationFrame(loop);
    };
    this.animFrameId = requestAnimationFrame(loop);
  }
}

export const audioEnergyService = new AudioEnergyService();

