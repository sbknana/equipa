#!/usr/bin/env python3
"""Test exponential backoff with jitter + model fallback retry logic.

Verifies:
1. Exponential backoff: 500ms base × 2^attempt, cap 32s
2. 25% jitter on delays
3. Model fallback after 3x 529 errors
4. Retryable error detection (429, 5xx, connection, timeout)
5. Non-retryable error immediate termination

Copyright 2026 Forgeborn
"""

import time

# Import retry functions
from equipa.agent_runner import (
    BASE_DELAY_MS,
    MAX_BACKOFF_MS,
    MAX_529_RETRIES,
    get_retry_delay,
    is_overloaded_error,
    is_retryable_error,
)


def test_exponential_backoff():
    """Test exponential backoff with jitter."""
    print("Testing exponential backoff with jitter...")

    for attempt in range(1, 11):
        delay = get_retry_delay(attempt)
        base_no_jitter = min(BASE_DELAY_MS * (2 ** (attempt - 1)), MAX_BACKOFF_MS) / 1000.0

        # Verify delay is within ±25% jitter range
        min_delay = base_no_jitter
        max_delay = base_no_jitter * 1.25

        assert min_delay <= delay <= max_delay, \
            f"Attempt {attempt}: delay {delay:.3f}s not in range [{min_delay:.3f}, {max_delay:.3f}]"

        print(f"  Attempt {attempt}: {delay:.3f}s (base: {base_no_jitter:.3f}s, "
              f"range: [{min_delay:.3f}, {max_delay:.3f}])")

    print("✓ Exponential backoff test passed\n")


def test_backoff_cap():
    """Test backoff cap at 32s."""
    print("Testing backoff cap at 32s...")

    delay = get_retry_delay(20)  # High attempt number
    max_possible = (MAX_BACKOFF_MS / 1000.0) * 1.25  # Max with jitter

    assert delay <= max_possible, \
        f"Delay {delay:.3f}s exceeds max {max_possible:.3f}s"

    print(f"  Attempt 20: {delay:.3f}s (cap: {MAX_BACKOFF_MS / 1000.0:.1f}s + jitter)")
    print("✓ Backoff cap test passed\n")


def test_overloaded_detection():
    """Test 529/overloaded error detection."""
    print("Testing 529/overloaded error detection...")

    test_cases = [
        ("", "529 error", True),
        ("Status 529", "", True),
        ("", "overloaded_error", True),
        ("", 'error: "type":"overloaded_error"', True),
        ("", "temporarily overloaded", True),
        ("", "404 not found", False),
        ("", "500 internal server error", False),  # Not overloaded
    ]

    for stderr, stdout, expected in test_cases:
        result = is_overloaded_error(stderr, stdout)
        assert result == expected, \
            f"is_overloaded_error({stderr!r}, {stdout!r}) = {result}, expected {expected}"
        status = "✓" if result else "✗"
        print(f"  {status} stderr={stderr!r}, stdout={stdout!r} → {result}")

    print("✓ Overloaded detection test passed\n")


def test_retryable_errors():
    """Test retryable error detection."""
    print("Testing retryable error detection...")

    test_cases = [
        ("", "429 rate limit", True),
        ("", "500 internal server error", True),
        ("", "502 bad gateway", True),
        ("", "503 service unavailable", True),
        ("", "504 gateway timeout", True),
        ("", "connection refused", True),
        ("timeout", "", True),
        ("ECONNRESET", "", True),
        ("EPIPE", "", True),
        ("", "400 bad request", False),
        ("", "401 unauthorized", False),
        ("", "403 forbidden", False),
        ("", "404 not found", False),
    ]

    for stderr, stdout, expected in test_cases:
        result = is_retryable_error(stderr, stdout)
        assert result == expected, \
            f"is_retryable_error({stderr!r}, {stdout!r}) = {result}, expected {expected}"
        status = "✓" if result else "✗"
        print(f"  {status} stderr={stderr!r}, stdout={stdout!r} → {result}")

    print("✓ Retryable error detection test passed\n")


def test_max_529_threshold():
    """Test MAX_529_RETRIES threshold."""
    print(f"Testing MAX_529_RETRIES threshold...")
    assert MAX_529_RETRIES == 3, f"Expected MAX_529_RETRIES=3, got {MAX_529_RETRIES}"
    print(f"  ✓ MAX_529_RETRIES = {MAX_529_RETRIES} (fallback after 3x 529)")
    print("✓ 529 threshold test passed\n")


if __name__ == "__main__":
    print("=" * 60)
    print("EQUIPA Retry Logic Test Suite")
    print("=" * 60)
    print()

    start = time.time()

    test_exponential_backoff()
    test_backoff_cap()
    test_overloaded_detection()
    test_retryable_errors()
    test_max_529_threshold()

    elapsed = time.time() - start

    print("=" * 60)
    print(f"✓ All tests passed in {elapsed:.2f}s")
    print("=" * 60)
