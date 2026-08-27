"""Backend domain primitives for private, evidence-grounded meeting queries."""

from .context import EvidenceSnapshot, InnerOSContextSnapshot, build_context_snapshot

__all__ = ["EvidenceSnapshot", "InnerOSContextSnapshot", "build_context_snapshot"]
