import { useLayoutEffect, useRef, useState } from "react";

import type { AssistantPhase } from "../stores/assistantStore";

export const LISTENING_TO_THINKING_MIN_VISIBLE_MS = 240;

export function getAssistantPhaseTransitionDelay(
  displayedPhase: AssistantPhase,
  nextPhase: AssistantPhase,
  phaseStartedAt: number,
  now = Date.now(),
): number {
  if (displayedPhase !== "listening" || nextPhase !== "thinking") return 0;
  const elapsed = Math.max(0, now - phaseStartedAt);
  return Math.max(0, LISTENING_TO_THINKING_MIN_VISIBLE_MS - elapsed);
}

interface DisplayedPhaseInput {
  readonly displayedPhase: AssistantPhase;
  readonly pipelinePhase: AssistantPhase;
  readonly observedSpeechSequence: number;
  readonly speechSequence: number;
  readonly phaseStartedAt: number;
  readonly now: number;
}

interface DisplayedPhaseTransition {
  readonly displayedPhase: AssistantPhase;
  readonly observedSpeechSequence: number;
  readonly delayMs: number;
}

export function getNextDisplayedPhase(input: DisplayedPhaseInput): DisplayedPhaseTransition {
  if (input.speechSequence !== input.observedSpeechSequence) {
    return {
      displayedPhase: "listening",
      observedSpeechSequence: input.speechSequence,
      delayMs: input.pipelinePhase === "listening"
        ? 0
        : LISTENING_TO_THINKING_MIN_VISIBLE_MS,
    };
  }
  return {
    displayedPhase: input.pipelinePhase,
    observedSpeechSequence: input.observedSpeechSequence,
    delayMs: getAssistantPhaseTransitionDelay(
      input.displayedPhase,
      input.pipelinePhase,
      input.phaseStartedAt,
      input.now,
    ),
  };
}

export function useDisplayedAssistantPhase(
  phase: AssistantPhase,
  speechSequence: number,
): AssistantPhase {
  const [displayedPhase, setDisplayedPhase] = useState(phase);
  const phaseStartedAtRef = useRef(Date.now());
  const observedSpeechSequenceRef = useRef(speechSequence);

  useLayoutEffect(() => {
    const now = Date.now();
    const transition = getNextDisplayedPhase({
      displayedPhase,
      pipelinePhase: phase,
      observedSpeechSequence: observedSpeechSequenceRef.current,
      speechSequence,
      phaseStartedAt: phaseStartedAtRef.current,
      now,
    });
    observedSpeechSequenceRef.current = transition.observedSpeechSequence;
    if (transition.displayedPhase === displayedPhase) return;
    if (transition.delayMs === 0 || transition.displayedPhase === "listening") {
      phaseStartedAtRef.current = now;
      setDisplayedPhase(transition.displayedPhase);
      return;
    }
    const timer = window.setTimeout(() => {
      phaseStartedAtRef.current = Date.now();
      setDisplayedPhase(transition.displayedPhase);
    }, transition.delayMs);
    return () => window.clearTimeout(timer);
  }, [displayedPhase, phase, speechSequence]);

  return displayedPhase;
}
