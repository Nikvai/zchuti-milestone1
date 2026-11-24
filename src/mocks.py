"""MOCKS used by tests and demos. Everything here is explicitly a mock.

FlakyLoader simulates a rules source that fails temporarily, so that retries,
timeouts and controlled stops can be tested without any external service.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict

from .tools import DEFAULT_RULES_PATH, TransientError, _load_file


def real_loader() -> Dict[str, Any]:
    return _load_file(DEFAULT_RULES_PATH)


class FlakyLoader:
    """Fails `failures` times (or always when failures is None), then works."""

    def __init__(self, failures=None, mode: str = "transient", delay_s: float = 0.0,
                 inner: Callable[[], Dict[str, Any]] = real_loader) -> None:
        self.failures = failures
        self.mode = mode  # "transient" raises TransientError, "slow" sleeps delay_s
        self.delay_s = delay_s
        self.inner = inner
        self.calls = 0

    def __call__(self) -> Dict[str, Any]:
        self.calls += 1
        if self.failures is None or self.calls <= self.failures:
            if self.mode == "slow":
                time.sleep(self.delay_s)
            else:
                raise TransientError("המאגר אינו זמין זמנית")
        return self.inner()
