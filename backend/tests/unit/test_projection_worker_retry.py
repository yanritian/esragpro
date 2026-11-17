"""Unit tests for exponential backoff policy (P1-04).

Tests validation, attempt progression, jitter, and caps.
"""

from __future__ import annotations

import pytest
import random

from app.memory_core.projection_worker.retry import ExponentialBackoffPolicy


class TestExponentialBackoffPolicyValidation:
    """Tests for policy validation."""

    def test_rejects_zero_base_delay(self) -> None:
        """Policy should reject zero base delay."""
        with pytest.raises(ValueError, match="base_delay_seconds must be positive"):
            ExponentialBackoffPolicy(base_delay_seconds=0.0)

    def test_rejects_negative_base_delay(self) -> None:
        """Policy should reject negative base delay."""
        with pytest.raises(ValueError, match="base_delay_seconds must be positive"):
            ExponentialBackoffPolicy(base_delay_seconds=-1.0)

    def test_rejects_multiplier_less_than_one(self) -> None:
        """Policy should reject multiplier < 1.0."""
        with pytest.raises(ValueError, match="multiplier must be >= 1.0"):
            ExponentialBackoffPolicy(base_delay_seconds=1.0, multiplier=0.5)

    def test_accepts_multiplier_equals_one(self) -> None:
        """Policy should accept multiplier == 1.0."""
        policy = ExponentialBackoffPolicy(base_delay_seconds=1.0, multiplier=1.0)
        assert policy.compute_delay(1) == 1.0

    def test_rejects_max_delay_less_than_base(self) -> None:
        """Policy should reject max_delay < base_delay."""
        with pytest.raises(ValueError, match="max_delay_seconds must be >= base_delay_seconds"):
            ExponentialBackoffPolicy(base_delay_seconds=10.0, max_delay_seconds=5.0)

    def test_accepts_max_delay_equals_base(self) -> None:
        """Policy should accept max_delay == base_delay."""
        policy = ExponentialBackoffPolicy(base_delay_seconds=5.0, max_delay_seconds=5.0)
        # All attempts should return base delay
        assert policy.compute_delay(1) == 5.0
        assert policy.compute_delay(10) == 5.0

    def test_rejects_negative_jitter(self) -> None:
        """Policy should reject negative jitter."""
        with pytest.raises(ValueError, match="jitter_fraction must be in"):
            ExponentialBackoffPolicy(base_delay_seconds=1.0, jitter_fraction=-0.1)

    def test_rejects_jitter_greater_than_one(self) -> None:
        """Policy should reject jitter >= 1."""
        with pytest.raises(ValueError, match="jitter_fraction must be in"):
            ExponentialBackoffPolicy(base_delay_seconds=1.0, jitter_fraction=1.0)

    def test_accepts_zero_jitter(self) -> None:
        """Policy should accept zero jitter."""
        policy = ExponentialBackoffPolicy(base_delay_seconds=1.0, jitter_fraction=0.0)
        assert policy.jitter_fraction == 0.0


class TestExponentialBackoffPolicyDelay:
    """Tests for delay computation."""

    def test_attempt_one_returns_base_delay(self) -> None:
        """Attempt 1 should return base delay."""
        policy = ExponentialBackoffPolicy(base_delay_seconds=2.0, multiplier=2.0)
        assert policy.compute_delay(1) == 2.0

    def test_attempt_two_multiplies_once(self) -> None:
        """Attempt 2 should multiply once."""
        policy = ExponentialBackoffPolicy(base_delay_seconds=2.0, multiplier=2.0)
        assert policy.compute_delay(2) == 4.0

    def test_attempt_three_multiplies_twice(self) -> None:
        """Attempt 3 should multiply twice."""
        policy = ExponentialBackoffPolicy(base_delay_seconds=2.0, multiplier=2.0)
        assert policy.compute_delay(3) == 8.0

    def test_delay_is_capped_at_max(self) -> None:
        """Delay should never exceed max_delay."""
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=1.0,
            multiplier=2.0,
            max_delay_seconds=10.0,
        )
        # Without cap: 1, 2, 4, 8, 16, 32...
        # With cap: 1, 2, 4, 8, 10, 10...
        assert policy.compute_delay(4) == 8.0  # Would be 16 without cap
        assert policy.compute_delay(5) == 10.0  # Capped
        assert policy.compute_delay(10) == 10.0  # Still capped

    def test_delay_never_negative(self) -> None:
        """Delay should never be negative."""
        policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)
        # This should never happen, but test the invariant
        delay = policy.compute_delay(1)
        assert delay >= 0.0

    def test_rejects_attempt_zero(self) -> None:
        """Policy should reject attempt < 1."""
        policy = ExponentialBackoffPolicy(base_delay_seconds=1.0)
        with pytest.raises(ValueError, match="attempt_count must be >= 1"):
            policy.compute_delay(0)


class TestExponentialBackoffPolicyJitter:
    """Tests for jitter behavior."""

    def test_jitter_is_deterministic_with_fixed_random(self) -> None:
        """Jitter should be deterministic with injected random source."""
        # Create policy with fixed random seed
        rng = random.Random(42)
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=10.0,
            jitter_fraction=0.2,
            random=rng,
        )

        # Multiple calls should be deterministic
        delay1 = policy.compute_delay(1)
        rng2 = random.Random(42)
        policy2 = ExponentialBackoffPolicy(
            base_delay_seconds=10.0,
            jitter_fraction=0.2,
            random=rng2,
        )
        delay2 = policy2.compute_delay(1)

        assert delay1 == delay2

    def test_jitter_stays_within_bounds(self) -> None:
        """Jitter should keep delay within expected bounds."""
        base = 10.0
        jitter_fraction = 0.2
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=base,
            jitter_fraction=jitter_fraction,
        )

        # Without jitter: delay = base
        # With 20% jitter: delay in [base * 0.8, base * 1.2]
        for _ in range(100):
            delay = policy.compute_delay(1)
            assert base * 0.8 <= delay <= base * 1.2

    def test_jitter_never_makes_delay_negative(self) -> None:
        """Jitter should never make delay negative."""
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=1.0,
            jitter_fraction=0.5,  # Large jitter
        )

        for _ in range(100):
            delay = policy.compute_delay(1)
            assert delay >= 0.0


class TestExponentialBackoffPolicyRealistic:
    """Tests for realistic usage scenarios."""

    def test_realistic_retry_schedule(self) -> None:
        """Test realistic retry schedule with default parameters."""
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=1.0,
            multiplier=2.0,
            max_delay_seconds=60.0,
        )

        # Expected: 1, 2, 4, 8, 16, 32, 60, 60...
        expected = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]

        for attempt, expected_delay in enumerate(expected, start=1):
            actual = policy.compute_delay(attempt)
            assert actual == expected_delay, f"Attempt {attempt}: expected {expected_delay}, got {actual}"

    def test_no_jitter_produces_exact_values(self) -> None:
        """Without jitter, values should be exact."""
        policy = ExponentialBackoffPolicy(
            base_delay_seconds=2.0,
            multiplier=1.5,
            max_delay_seconds=100.0,
            jitter_fraction=0.0,
        )

        # No randomness, should be exact
        for attempt in range(1, 10):
            delay1 = policy.compute_delay(attempt)
            delay2 = policy.compute_delay(attempt)
            assert delay1 == delay2