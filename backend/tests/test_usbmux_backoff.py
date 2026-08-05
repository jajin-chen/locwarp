"""Tests for core.device_manager.UsbmuxAvailability."""

from core.device_manager import UsbmuxAvailability


def test_fresh_failure_reports_once_and_backs_off() -> None:
    a = UsbmuxAvailability(cooldown=60.0)
    assert a.should_attempt(now=0.0) is True
    assert a.record_failure(now=0.0) is True    # fresh outage → log it
    assert a.should_attempt(now=30.0) is False  # inside cooldown
    assert a.should_attempt(now=60.0) is True   # cooldown elapsed
    assert a.record_failure(now=60.0) is False  # still down → stay quiet


def test_recovery_resets_state() -> None:
    a = UsbmuxAvailability(cooldown=60.0)
    a.record_failure(now=0.0)
    assert a.record_success() is True           # was down → recovered
    assert a.record_success() is False          # already up → quiet
    assert a.should_attempt(now=1.0) is True
    assert a.record_failure(now=1.0) is True    # next outage logs again
