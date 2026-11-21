"""Shared live delivery window; shadow experiments keep their own timing."""

MIN_LEAD = 15
MAX_LEAD = 20


def in_window(entry_at, now):
    return MIN_LEAD <= entry_at - now <= MAX_LEAD


def send_timeout(entry_at, now):
    """Never extend a Telegram request beyond the actionable deadline."""
    return max(0.0, min(3.0, entry_at - MIN_LEAD - now))
