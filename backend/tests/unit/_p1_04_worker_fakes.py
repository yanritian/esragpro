"""Fake projection handlers for P1-04 unit tests.

Demonstrates idempotent handler contract with side-effect tracking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.memory_core.projection_worker.contracts import (
    ProjectionEvent,
    PermanentProjectionError,
)


@dataclass
class FakeTargetState:
    """Tracks state for a fake projection target."""

    calls: list[str] = field(default_factory=list)
    """List of idempotency keys that were processed."""

    successes: list[str] = field(default_factory=list)
    """List of idempotency keys that succeeded."""

    failures: list[str] = field(default_factory=list)
    """List of idempotency keys that failed."""

    permanent_failures: list[str] = field(default_factory=list)
    """List of idempotency keys that permanently failed."""

    timeouts: list[str] = field(default_factory=list)
    """List of idempotency keys that timed out."""

    def total_calls(self) -> int:
        """Total number of calls."""
        return len(self.calls)

    def unique_calls(self) -> int:
        """Number of unique idempotency keys."""
        return len(set(self.calls))


@dataclass
class FakeHandler:
    """Fake projection handler with configurable behavior.

    Tracks all calls and demonstrates idempotency contract:
    - Uses idempotency_key for dedup
    - Repeated delivery is valid
    - Records side effects only once per key
    """

    target_name: str
    state: FakeTargetState = field(default_factory=FakeTargetState)
    should_fail: bool = False
    should_fail_permanent: bool = False
    should_timeout: bool = False
    fail_on_keys: set[str] = field(default_factory=set)

    async def __call__(self, event: ProjectionEvent) -> None:
        """Handle the projection event.

        Args:
            event: Projection event envelope

        Raises:
            PermanentProjectionError: If configured for permanent failure
            Exception: If configured for retryable failure
        """
        key = event.idempotency_key

        # Record call (may be duplicate)
        self.state.calls.append(key)

        # Check for permanent failure
        if self.should_fail_permanent or key in self.fail_on_keys:
            self.state.permanent_failures.append(key)
            raise PermanentProjectionError(
                f"Permanent failure for {key}",
                target=self.target_name,
            )

        # Check for retryable failure
        if self.should_fail:
            self.state.failures.append(key)
            raise Exception(f"Retryable failure for {key}")

        # Check for timeout
        if self.should_timeout:
            self.state.timeouts.append(key)
            # Simulate timeout by sleeping
            import asyncio
            await asyncio.sleep(10)

        # Success - record only if first time
        if key not in self.state.successes:
            self.state.successes.append(key)


@dataclass
class FakeHandlerRegistry:
    """Registry of fake handlers for testing.

    Tracks handlers by target name and provides lookup.
    """

    handlers: dict[str, FakeHandler] = field(default_factory=dict)

    def get_handler(self, target: str) -> FakeHandler | None:
        """Get handler for target."""
        return self.handlers.get(target)

    def has_target(self, target: str) -> bool:
        """Check if target is registered."""
        return target in self.handlers

    def add_handler(self, handler: FakeHandler) -> None:
        """Add a handler."""
        self.handlers[handler.target_name] = handler

    @property
    def targets(self) -> tuple[str, ...]:
        """Get all registered targets."""
        return tuple(self.handlers.keys())


def make_fake_handler(
    target_name: str,
    **kwargs: Any,
) -> FakeHandler:
    """Create a fake handler with default state.

    Args:
        target_name: Name of projection target
        **kwargs: Additional FakeHandler arguments

    Returns:
        FakeHandler instance
    """
    return FakeHandler(target_name=target_name, **kwargs)


__all__ = [
    "FakeTargetState",
    "FakeHandler",
    "FakeHandlerRegistry",
    "make_fake_handler",
]