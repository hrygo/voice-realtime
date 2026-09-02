"""Compatibility import for the shared local-inference scheduler."""

from sona.inference.scheduler import LocalInferenceScheduler

LocalLLMWorkloadGate = LocalInferenceScheduler

__all__ = ["LocalLLMWorkloadGate"]
