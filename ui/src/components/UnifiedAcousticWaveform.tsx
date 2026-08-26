import { useEffect, useRef } from "react";
import { audioEnergyService } from "../services/audioEnergyService";
import type { AssistantPhase } from "../stores/assistantStore";

export type AcousticWaveformTheme = "assistant" | "subtitle" | "meeting";

export type AcousticWaveformState = AssistantPhase | "recording";


interface UnifiedAcousticWaveformProps {
  readonly themePreset: AcousticWaveformTheme;
  readonly state?: AcousticWaveformState;
  readonly isMuted?: boolean;
  readonly activeTextTrigger?: unknown;
  readonly className?: string;
  readonly ariaLabel?: string;
}

interface WaveParticle {
  x: number;
  y: number;
  radius: number;
  alpha: number;
  speedY: number;
  phaseOffset: number;
}

/**
 * 统一多谐波拟真声学波形引擎：
 * 采用【Silero VAD 毫秒级拾音即时声浪 + 麦克风频域时域双通道能量 + 流式共振】多维声学生态驱动，
 * 实现开口即波动（10ms 零延迟）、音量大小精准映射、停嘴自然平息的高保真视觉交互体验。
 */
export function UnifiedAcousticWaveform({

  themePreset,
  state = "idle",
  isMuted = false,
  activeTextTrigger,
  className = "",
  ariaLabel = "动态声学能量波形",
}: UnifiedAcousticWaveformProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const themeRef = useRef(themePreset);
  const stateRef = useRef(state);
  const mutedRef = useRef(isMuted);

  themeRef.current = themePreset;
  stateRef.current = state;
  mutedRef.current = isMuted;

  // 内部平滑能量物理量
  const energyRef = useRef(0.08);

  // ASR 文本流式打字声学脉冲时间戳
  const textPulseUntilRef = useRef<number>(0);
  const prevTriggerRef = useRef<unknown>(activeTextTrigger);

  // 监听文字流更新：只要文字在打出，立刻注入 1200ms 的人声高能共振脉冲
  useEffect(() => {
    if (activeTextTrigger !== undefined && activeTextTrigger !== null) {
      if (activeTextTrigger !== prevTriggerRef.current) {
        prevTriggerRef.current = activeTextTrigger;
        textPulseUntilRef.current = Date.now() + 1400; // 持续 1.4 秒高能激荡
      }
    }
  }, [activeTextTrigger]);

  useEffect(() => {
    audioEnergyService.setMuted(isMuted);
  }, [isMuted]);

  useEffect(() => {
    // 订阅全局麦克风真实声音大小
    const unsubscribe = audioEnergyService.subscribe((realAudioEnergy) => {
      energyRef.current = realAudioEnergy;
    });

    return () => {
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || typeof canvas.getContext !== "function") return;
    let ctx: CanvasRenderingContext2D | null = null;
    try {
      ctx = canvas.getContext("2d");
    } catch {
      return;
    }
    if (!ctx) return;

    let animFrame = 0;
    let tick = 0;
    let cachedWidth = 0;
    let cachedHeight = 0;
    let isRunning = true;
    let smoothedEnergy = 0.08;

    // 动态能量微粒子（随真实声音大小扩散）
    const particleCount = 20;
    const particles: WaveParticle[] = Array.from({ length: particleCount }, () => ({
      x: Math.random(),
      y: 0.5 + (Math.random() - 0.5) * 0.4,
      radius: 0.8 + Math.random() * 1.6,
      alpha: 0.2 + Math.random() * 0.6,
      speedY: 0.002 + Math.random() * 0.004,
      phaseOffset: Math.random() * Math.PI * 2,
    }));

    const resize = () => {
      const bounds = canvas.getBoundingClientRect();
      cachedWidth = bounds.width;
      cachedHeight = bounds.height;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, Math.round(cachedWidth * dpr));
      canvas.height = Math.max(1, Math.round(cachedHeight * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const render = () => {
      if (!isRunning) return;

      const currentState = stateRef.current;
      const muted = mutedRef.current;
      const theme = themeRef.current;
      const realAudioEnergy = energyRef.current;
      const isTextPulsing = Date.now() < textPulseUntilRef.current;

      // 文本脉冲衰减
      let textEnergy = 0;
      if (isTextPulsing) {
        const remaining = Math.max(0, textPulseUntilRef.current - Date.now());
        textEnergy = (remaining / 1400) * 0.75;
      }

      // 计算当前真实综合能量目标
      let targetEnergy = 0.08;

      if (muted) {
        targetEnergy = 0.02;
      } else if (currentState === "speaking") {
        // 播报 / 转写吐字中：饱满共振声浪与自然语调呼吸
        const speakingCadence = 0.38 + Math.sin(tick * 1.5) * 0.12;
        targetEnergy = Math.max(realAudioEnergy, textEnergy, speakingCadence);
      } else if (currentState === "listening") {
        // 聆听麦克风：以真实麦克风能量为主导，静默待命时维持温润微波，有声时敏锐激荡
        const listeningBaseline = 0.08 + Math.sin(tick * 0.8) * 0.02;
        targetEnergy = Math.max(realAudioEnergy, textEnergy, listeningBaseline);
      } else if (currentState === "thinking") {
        // 思考中：规律柔和神经突触微脉冲
        const thinkingPulse = 0.32 + Math.sin(tick * 2.0) * 0.10;
        targetEnergy = Math.max(realAudioEnergy, textEnergy, thinkingPulse);
      } else {
        // 待命静息（idle / stopped / degraded / recording）：跟随真实麦克风输入，无声时维持平稳温润基线
        targetEnergy = Math.max(realAudioEnergy, textEnergy, 0.08);
      }

      smoothedEnergy += (targetEnergy - smoothedEnergy) * 0.18;

      // 流动速度：静默时优雅平缓 (0.016)，有声时自然灵动加速
      tick += 0.016 + smoothedEnergy * 0.025;

      if (cachedWidth > 0 && cachedHeight > 0) {
        ctx.clearRect(0, 0, cachedWidth, cachedHeight);



        const centerY = cachedHeight / 2;
        const width = cachedWidth;
        const maxAmplitude = Math.min(cachedHeight * 0.48, 22);

        // 统一主题色彩体系
        let primaryColor = "rgba(99, 102, 241, ";
        let secondaryColor = "rgba(168, 85, 247, ";
        let accentColor = "rgba(34, 211, 238, ";

        if (muted) {
          primaryColor = secondaryColor = accentColor = "rgba(239, 68, 68, ";
        } else if (theme === "subtitle") {
          // 字幕模式：琥珀金与翡翠青
          primaryColor = "rgba(245, 158, 11, ";
          secondaryColor = "rgba(251, 191, 36, ";
          accentColor = "rgba(52, 211, 153, ";
        } else if (theme === "meeting") {
          // 会议模式：深海蓝宝石与翡翠青
          primaryColor = "rgba(59, 130, 246, ";
          secondaryColor = "rgba(6, 182, 212, ";
          accentColor = "rgba(16, 185, 129, ";
        } else {
          // 语音助手模式：极光紫蓝与状态联动
          if (currentState === "thinking") {
            primaryColor = "rgba(245, 158, 11, ";
            secondaryColor = "rgba(236, 72, 153, ";
            accentColor = "rgba(251, 191, 36, ";
          } else if (currentState === "speaking") {
            primaryColor = "rgba(99, 102, 241, ";
            secondaryColor = "rgba(168, 85, 247, ";
            accentColor = "rgba(6, 182, 212, ";
          } else {
            // listening / idle
            primaryColor = "rgba(99, 102, 241, ";
            secondaryColor = "rgba(168, 85, 247, ";
            accentColor = "rgba(34, 211, 238, ";
          }
        }

        // 1. 绘制第一层：柔和微光底波（Fluid Under-Wave）
        ctx.save();
        ctx.beginPath();
        const underPoints = 48;
        const underStep = width / (underPoints - 1);
        for (let i = 0; i < underPoints; i++) {
          const x = i * underStep;
          const normX = (x / width) * 2 - 1;
          const windowEnvelope = Math.cos(normX * Math.PI * 0.46);
          const wave1 = Math.sin(x * 0.02 + tick * 0.8) * 0.65;
          const wave2 = Math.sin(x * 0.04 - tick * 1.2) * 0.45;
          const y = centerY + (wave1 + wave2) * maxAmplitude * smoothedEnergy * 1.1 * windowEnvelope;
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = secondaryColor + (0.35 + smoothedEnergy * 0.45) + ")";
        ctx.lineWidth = 2.4 + smoothedEnergy * 1.2;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.stroke();
        ctx.restore();

        // 2. 绘制第二层：主声学谐振波（Primary Wave，振幅随真实音量大小极度敏锐放大）
        ctx.save();
        ctx.beginPath();
        const mainPoints = 64;
        const mainStep = width / (mainPoints - 1);
        for (let i = 0; i < mainPoints; i++) {
          const x = i * mainStep;
          const normX = (x / width) * 2 - 1;
          const windowEnvelope = Math.cos(normX * Math.PI * 0.46);
          const harmonic1 = Math.sin(x * 0.024 - tick * 1.1) * 0.75;
          const harmonic2 = Math.sin(x * 0.048 + tick * 1.5) * 0.35;
          const harmonic3 = Math.cos(x * 0.012 + tick * 0.6) * 0.25;
          const y = centerY + (harmonic1 + harmonic2 + harmonic3) * maxAmplitude * smoothedEnergy * 1.45 * windowEnvelope;
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        const grad = ctx.createLinearGradient(0, 0, width, 0);
        grad.addColorStop(0, primaryColor + "0.35)");
        grad.addColorStop(0.25, primaryColor + (0.75 + smoothedEnergy * 0.25) + ")");
        grad.addColorStop(0.5, secondaryColor + "1)");
        grad.addColorStop(0.75, accentColor + (0.75 + smoothedEnergy * 0.25) + ")");
        grad.addColorStop(1, accentColor + "0.35)");

        ctx.strokeStyle = grad;
        ctx.lineWidth = 3.0 + smoothedEnergy * 1.6;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.shadowColor = secondaryColor + "0.9)";
        ctx.shadowBlur = smoothedEnergy > 0.15 ? 16 : 4;
        ctx.stroke();
        ctx.restore();


        // 3. 绘制第三层：微光能量粒子（有声时随音量浮游呼吸）
        if (smoothedEnergy > 0.1) {
          ctx.save();
          particles.forEach((p) => {
            p.y += Math.sin(tick * 0.8 + p.phaseOffset) * 0.003;
            const px = p.x * width;
            const normX = (px / width) * 2 - 1;
            const windowEnvelope = Math.cos(normX * Math.PI * 0.46);
            const waveY = Math.sin(px * 0.024 - tick * 1.1) * maxAmplitude * smoothedEnergy * windowEnvelope;
            const py = centerY + waveY + (p.y - 0.5) * 14;

            ctx.beginPath();
            ctx.arc(px, py, p.radius, 0, Math.PI * 2);
            ctx.fillStyle = accentColor + (p.alpha * Math.min(1, smoothedEnergy * 1.8)) + ")";
            ctx.shadowColor = accentColor + "0.6)";
            ctx.shadowBlur = 5;
            ctx.fill();
          });
          ctx.restore();
        }
      }


      animFrame = requestAnimationFrame(render);
    };

    const handleVisibilityChange = () => {
      if (document.hidden) {
        isRunning = false;
        cancelAnimationFrame(animFrame);
      } else if (!isRunning) {
        isRunning = true;
        animFrame = requestAnimationFrame(render);
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    resize();
    if (!document.hidden) animFrame = requestAnimationFrame(render);
    else isRunning = false;

    return () => {
      isRunning = false;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      observer.disconnect();
      cancelAnimationFrame(animFrame);
    };
  }, []);

  return (
    <canvas
      className={`unified-acoustic-waveform-canvas ${className}`}
      ref={canvasRef}
      role="img"
      aria-label={ariaLabel}
    />
  );
}
