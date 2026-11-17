"""Capped exponential backoff retry policy (P1-04).

Validated, independently testable policy for computing retry delays.
"""

from __future__ import annotations

import math
import random as random_module
from dataclasses import dataclass, field


def _validate_finite(name: str, value: float) -> None:
    """Validate that a numeric value is finite (not NaN or infinity)."""
    if math.isnan(value):
        raise ValueError(f"{name} must not be NaN")
    if math.isinf(value):
        raise ValueError(f"{name} must not be infinite")


@dataclass
class ExponentialBackoffPolicy:
    """Capped exponential backoff with optional jitter.

    All parameters validated at construction.
    Attempt 1 maps to the base-delay range.
    Delay never exceeds cap and is never negative.
    """

    base_delay_seconds: float
    multiplier: float = 2.0
    max_delay_seconds: float = 300.0
    jitter_fraction: float = 0.0
    random: random_module.Random = field(
        default_factory=random_module.Random, repr=False
    )

    def __post_init__(self) -> None:
        # Validate parameters
        _validate_finite("base_delay_seconds", self.base_delay_seconds)
        _validate_finite("multiplier", self.multiplier)
        _validate_finite("max_delay_seconds", self.max_delay_seconds)
        _validate_finite("jitter_fraction", self.jitter_fraction)

        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be positive")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError(
                "max_delay_seconds must be >= base_delay_seconds"
            )
        if not (0.0 <= self.jitter_fraction < 1.0):
            raise ValueError("jitter_fraction must be in [0, 1)")

    def compute_delay(self, attempt_count: int) -> float:
        """Compute delay in seconds for the given attempt.

        Args:
            attempt_count: Current attempt (1-indexed)

        Returns:
            Delay in seconds (non-negative, capped)
        """
        if attempt_count < 1:
            raise ValueError("attempt_count must be >= 1")

        # Attempt 1 -> base_delay, attempt 2 -> base_delay * multiplier, etc.
        # Use saturating exponentiation without overflow
        if attempt_count == 1:
            raw_delay = self.base_delay_seconds
        elif self.multiplier <= 1.0:
            # multiplier == 1.0, no exponentiation needed
            raw_delay = self.base_delay_seconds
        else:
            # Check if we've already saturated the cap
            # If base_delay >= max_delay, we're already at cap
            if self.base_delay_seconds >= self.max_delay_seconds:
                raw_delay = self.max_delay_seconds
            else:
                # Use log-space comparison to avoid overflow with subnormal base_delay
                # We want to know if base * multiplier^(attempt-1) >= max_delay
                # Equivalent to: multiplier^(attempt-1) >= max_delay / base
                # In log space: (attempt-1) * log(multiplier) >= log(max_delay) - log(base)
                log_multiplier = math.log(self.multiplier)
                log_max = math.log(self.max_delay_seconds)
                log_base = math.log(self.base_delay_seconds)

                # The exponent for attempt N is (N - 1)
                exponent = attempt_count - 1

                # Compare in log space to detect saturation
                # If (attempt-1) * log(multiplier) >= log(max_delay) - log(base)
                # then we've saturated
                if exponent * log_multiplier >= log_max - log_base:
                    raw_delay = self.max_delay_seconds
                else:
                    # Safe to compute without overflow - we know result is finite
                    raw_delay = self.base_delay_seconds * (self.multiplier ** exponent)

        # Apply jitter BEFORE cap
        if self.jitter_fraction > 0.0:
            jitter_range = raw_delay * self.jitter_fraction
            jitter = self.random.uniform(-jitter_range, jitter_range)
            raw_delay = raw_delay + jitter

        # Apply cap AFTER jitter to ensure we never exceed max_delay
        capped_delay = min(raw_delay, self.max_delay_seconds)

        # Ensure non-negative
        capped_delay = max(0.0, capped_delay)

        return capped_delay


__all__ = ["ExponentialBackoffPolicy"]
