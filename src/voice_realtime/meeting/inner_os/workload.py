"""Compatibility import for the shared local-inference scheduler."""

from voice_realtime.inference.scheduler import LocalInferenceScheduler

LocalLLMWorkloadGate = LocalInferenceScheduler

__all__ = ["LocalLLMWorkloadGate"]
