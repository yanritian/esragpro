"""Typed adapter errors for the world-memory projection payload adapters (P6-D).

A :class:`WorldMemoryProjectionAdapterError` is raised by the ES and Neo4j
adapters when the validated envelope cannot be converted to a target payload.
"""

from __future__ import annotations


class WorldMemoryProjectionAdapterError(ValueError):
    """Raised when a validated envelope cannot be adapted to a target payload.

    The ``kind`` attribute names the snapshot kind discriminator (e.g.
    ``"observation"``) when the failure is kind-specific, or ``None`` when
    the failure is structural (e.g. missing required field, unsupported kind).
    """

    def __init__(self, message: str, *, kind: str | None = None) -> None:
        self.kind = kind
        super().__init__(message)
