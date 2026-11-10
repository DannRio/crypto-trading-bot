"""Stable setup identities and event metadata; not probability estimates."""
from dataclasses import replace
from hashlib import sha256

import pandas as pd


def attach_event(assessment, df, *, setup_type, anchor, invalidation,
                 event_index=-1, target=None, cancel=None):
    row = df.iloc[event_index]
    # Production frames always have timestamps. The index fallback supports unit fixtures.
    stamp = row.get("timestamp", df.index[event_index])
    stamp = pd.Timestamp(stamp).isoformat() if isinstance(stamp, (pd.Timestamp, str)) else str(stamp)
    identity = "|".join((assessment.strategy, assessment.signal.value, setup_type,
                         stamp, format(float(anchor), ".12g")))
    return replace(assessment, event_id=sha256(identity.encode()).hexdigest()[:24],
                   setup_type=setup_type, event_time=stamp, anchor_price=float(anchor),
                   invalidation_price=float(invalidation), target_price=target,
                   cancel_price=float(cancel if cancel is not None else invalidation))
