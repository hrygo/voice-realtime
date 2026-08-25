import { useEffect, useRef } from "react";

import type { AssistantPhase } from "../stores/assistantStore";
import "./AssistantStatus.css";

/** 状态感知、页面不可见时暂停的助手波形。 */
export function AssistantWaveform({
  phase,
  isMuted,
}: {
  readonly phase: AssistantPhase;
  readonly isMuted: boolean;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const phaseRef = useRef(phase);
  const mutedRef = useRef(isMuted);
  phaseRef.current = phase;
  mutedRef.current = isMuted;

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    let animFrame = 0;
    let tick = 0;
    let lastTime = 0;
    let cachedWidth = 0;
    let cachedHeight = 0;
    let isRunning = true;
    const barCount = 34;
    const barLevels = new Array<number>(barCount).fill(4);
    const targetLevels = new Array<number>(barCount).fill(4);

    const resize = () => {
      const bounds = canvas.getBoundingClientRect();
      cachedWidth = bounds.width;
      cachedHeight = bounds.height;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.round(cachedWidth * dpr));
      canvas.height = Math.max(1, Math.round(cachedHeight * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const render = (currentTime: number) => {
      if (!isRunning) return;
      const currentPhase = phaseRef.current;
      const muted = mutedRef.current;
      const isActive = !muted
        && currentPhase !== "idle"
        && currentPhase !== "degraded"
        && currentPhase !== "stopped";
      const targetInterval = isActive ? 28 : 66;
      const elapsed = currentTime - lastTime;

      if (elapsed >= targetInterval) {
        lastTime = currentTime - (elapsed % targetInterval);
        tick += 1;
        if (cachedWidth > 0 && cachedHeight > 0) {
          for (let index = 0; index < barCount; index++) {
            if (muted) targetLevels[index] = 2;
            else if (["idle", "degraded", "stopped"].includes(currentPhase)) {
              targetLevels[index] = 3 + Math.sin(tick * 0.08 + index * 0.2) * 1.5;
            } else if (currentPhase === "listening") {
              targetLevels[index] = 4 + Math.random() * 24
                + Math.sin(index * 0.3 + tick * 0.2) * 6;
            } else if (currentPhase === "thinking") {
              targetLevels[index] = 6 + Math.sin(tick * 0.2 - index * 0.4) * 12;
            } else {
              targetLevels[index] = 6
                + Math.abs(Math.sin(tick * 0.15 + index * 0.25)) * 26
                + Math.random() * 8;
            }
          }

          ctx.clearRect(0, 0, cachedWidth, cachedHeight);
          const gradient = ctx.createLinearGradient(0, 0, cachedWidth, 0);
          if (muted) {
            gradient.addColorStop(0, "rgba(239, 68, 68, 0.4)");
            gradient.addColorStop(1, "rgba(239, 68, 68, 0.4)");
          } else if (currentPhase === "speaking") {
            gradient.addColorStop(0, "#6366f1");
            gradient.addColorStop(0.5, "#a855f7");
            gradient.addColorStop(1, "#06b6d4");
          } else if (currentPhase === "listening") {
            gradient.addColorStop(0, "#10b981");
            gradient.addColorStop(1, "#06b6d4");
          } else if (currentPhase === "thinking") {
            gradient.addColorStop(0, "#f59e0b");
            gradient.addColorStop(1, "#ec4899");
          } else {
            gradient.addColorStop(0, "rgba(148, 163, 184, 0.4)");
            gradient.addColorStop(1, "rgba(148, 163, 184, 0.6)");
          }
          ctx.fillStyle = gradient;
          const gap = cachedWidth / barCount;
          const barWidth = Math.max(2.5, gap * 0.52);
          for (let index = 0; index < barCount; index++) {
            barLevels[index] += (targetLevels[index] - barLevels[index]) * 0.25;
            const height = Math.max(2, barLevels[index]);
            const x = index * gap + (gap - barWidth) / 2;
            const y = (cachedHeight - height) / 2;
            ctx.beginPath();
            ctx.roundRect(x, y, barWidth, height, 2);
            ctx.fill();
          }
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
        lastTime = performance.now();
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
      className="assistant-waveform-canvas"
      ref={canvasRef}
      role="img"
      aria-label="声学动态频谱波形"
    />
  );
}
