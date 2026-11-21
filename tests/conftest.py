from __future__ import annotations

from functools import wraps

import pandas as pd
import pytest

from trading_bot.execution import ManagedLiveBrokerAdapter


_FIXED_LIVE_NOW = pd.Timestamp("2026-11-09T12:05:00Z")


@pytest.fixture(autouse=True)
def _freeze_legacy_live_execution_clock(monkeypatch, request) -> None:
    """Keep static live-execution intents deterministic as calendar time advances.

    The legacy pipeline tests use an intent that expires on 2026-11-10. Tests
    that do not inject a clock would eventually fail for the wrong reason. This
    fixture is intentionally scoped to that single test module and preserves an
    explicitly supplied ``now_fn``.
    """

    if request.node.path.name != "test_live_execution_pipeline.py":
        return

    original_init = ManagedLiveBrokerAdapter.__init__

    @wraps(original_init)
    def deterministic_init(self, *args, **kwargs):
        kwargs.setdefault("now_fn", lambda: _FIXED_LIVE_NOW)
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(
        ManagedLiveBrokerAdapter,
        "__init__",
        deterministic_init,
    )
