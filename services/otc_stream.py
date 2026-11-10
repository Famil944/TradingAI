"""Reconnect delays; cancellation remains asynchronous and immediate."""

import random


def reconnect_delay(attempt, retry_after=0):
    return max(retry_after, min(60, 2 ** min(attempt, 6)) + random.uniform(0, 1))
