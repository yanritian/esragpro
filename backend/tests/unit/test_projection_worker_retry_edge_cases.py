"""Unit tests for retry policy edge cases (P1-04).

Tests NaN, infinity, jitter-at-cap, and overflow protection.
"""

from __future__ import annotations

import math
import pytest
import random

from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy


class TestRetryPolicyNaNAndInfinity:
    """Tests for NaN and infinity rejection."""

    def test_rejects_nan_base_delay(self) -> None:
        """Policy should reject NaN base_delay_seconds."""
        with pytest.raises(ValueError, match="must not be NaN"):
            ExponentialBackoffPolicy(base_delay_seconds=float("nan"))

    def test_rejects_nan_multiplier(self) -> None:
        """Policy should reject NaN multiplier."""
        with pytest.raises(ValueError, match="must not be NaN"):
            ExponentialBackoffPolicy(
                base_delay_seconds=1.0,
                multiplier=float("nan")
            )

    def test_rejects_nan_max_delay(self) -> None:
        """Policy should reject NaN max_delay_seconds."""
        with pytest.raises(ValueError, match="must not be NaN"):
            ExponentialBackoffPolicy(
                base_delay_seconds=1.0,
                max_delay_seconds=float("nan")
            )

    def test_rejects_nan_jitter(self) -> None:
        """Policy should reject NaN jitter_fraction."""
        with pytest.raises(ValueError, match="must not be NaN"):
            ExponentialBackoffPolicy(
                base_delay_seconds=1.0,
                jitter_fraction=float("nan")
            )

    def test_rejects_infinity_base_delay(self) -> None:
        """Policy should reject infinite base_delay_seconds."""
        with pytest.raises(ValueError, match="must not be infinite"):
            ExponentialBackoffPolicy(base_delay_seconds=float("inf"))

    def test_rejects_negative_infinity_base_delay(self) -> None:
        """Policy should reject negative infinite base_delay_seconds."""
        with pytest.raises(ValueError, match="must not be infinite"):
            ExponentialBackoffPolicy(base_delay_seconds=float("-inf"))


class TestRetryPolicyJitterAtCap:
    """Tests for jitter behavior at cap."""

    def test_jitter_never_exceeds_max_delay(self) -> None:
        """Jitter should never make delay exceed max_delay_seconds."""
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=10.0,
            multiplier=2.0,
            max_delay_seconds=10.0,  # Cap at base
            jitter_fraction=0.5,  # 50% jitter
        )

        # Even with 50% jitter, delay should never exceed max
        for _ in range(100):
            delay = policy.compute_delay(1)
            assert delay <= 10.0, f"Delay {delay} exceeds max 10.0"

    def test_positive_jitter_applied_before_cap(self) -> None:
        """Positive jitter should be applied before capping."""
        # With fixed random that gives max positive jitter
        rng = random.Random(42)
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=10.0,
            max_delay_seconds=10.0,  # Explicitly set max to base
            jitter_fraction=0.2,  # 20% jitter = +2.0 max
            random=rng,
        )

        # Get first delay (will have jitter applied)
        delay = policy.compute_delay(1)

        # Delay should be capped at max_delay_seconds
        assert delay <= 10.0


class TestRetryPolicyOverflow:
    """Tests for overflow protection."""

    def test_large_attempt_count_no_overflow(self) -> None:
        """Large attempt count should not cause overflow."""
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=1.0,
            multiplier=2.0,
            max_delay_seconds=300.0,
        )

        # Very large attempt count
        delay = policy.compute_delay(1000)

        # Should be capped and not overflow
        assert delay == 300.0
        assert math.isfinite(delay)

    def test_very_large_multiplier_no_overflow(self) -> None:
        """Very large multiplier should not cause overflow."""
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=1.0,
            multiplier=100.0,  # Very large
            max_delay_seconds=300.0,
        )

        # Attempt 10 would be 100^9 without protection
        delay = policy.compute_delay(10)

        # Should be capped
        assert delay == 300.0
        assert math.isfinite(delay)


class TestRetryPolicySubnormalBase:
    """Tests for subnormal base_delay handling (P1-04 regression)."""

    def test_subnormal_base_delay_no_overflow(self) -> None:
        """Subnormal base_delay_seconds should not cause OverflowError.

        With base_delay=5e-324 (smallest positive denormalized float),
        computing max_delay/base_delay would overflow to infinity.
        The log-space comparison must prevent this.
        """
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=5e-324,  # Smallest positive subnormal
            max_delay_seconds=1.0,
            multiplier=2.0,
        )

        # Huge attempt count - would cause overflow without log-space fix
        # The ratio max_delay/base_delay = 1.0 / 5e-324 overflows to inf
        # In log space: log(1.0) - log(5e-324) is finite
        delay = policy.compute_delay(10000)

        # Should return finite max_delay, not raise OverflowError
        assert delay == 1.0
        assert math.isfinite(delay)

    def test_subnormal_base_delay_attempts_before_saturation(self) -> None:
        """Subnormal base with reasonable attempt count should compute correctly."""
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=1e-300,  # Subnormal range
            max_delay_seconds=1.0,
            multiplier=2.0,
        )

        # For base=1e-300, max=1.0, multiplier=2.0:
        # Saturation occurs when 2^(attempt-1) >= 1e300
        # log2(1e300) ≈ 997 attempts
        # So attempt 100 should be unsaturated and finite
        delay = policy.compute_delay(100)
        assert math.isfinite(delay)
        assert delay <= 1.0

        # Should still be exponential at this point
        # delay = 1e-300 * 2^99 ≈ 6e-271 (still very small)
        assert delay < 1.0

    def test_subnormal_base_preserves_exact_values_for_ordinary_cases(self) -> None:
        """Log-space optimization should preserve exact values for ordinary cases."""
        # Use a normal base_delay to verify behavior is unchanged
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=1.0,
            multiplier=2.0,
            max_delay_seconds=60.0,
            jitter_fraction=0.0,
        )

        # Verify ordinary progression is unchanged
        assert policy.compute_delay(1) == 1.0
        assert policy.compute_delay(2) == 2.0
        assert policy.compute_delay(3) == 4.0
        assert policy.compute_delay(4) == 8.0
        assert policy.compute_delay(5) == 16.0
        assert policy.compute_delay(6) == 32.0
        assert policy.compute_delay(7) == 60.0  # Capped


__all__ = [
    "TestRetryPolicyNaNAndInfinity",
    "TestRetryPolicyJitterAtCap",
    "TestRetryPolicyOverflow",
    "TestRetryPolicySubnormalBase",
]
