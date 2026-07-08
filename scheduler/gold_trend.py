"""Weekly gold trend analysis job.

Deterministic KRX-gold technical scoring + macro scorecard (live FX/real-yield +
yaml config for slow structural factors) + one optional LLM summary paragraph.
Emits an ACCUMULATE/HOLD/PAUSE verdict to Telegram and a report file.
"""

from __future__ import annotations

import copy
import pathlib
from typing import Optional

import yaml

GRAMS_PER_OZ = 31.1035

DEFAULT_CONFIG: dict = {
    "last_reviewed": "2026-07-04",
    "central_bank": {"trailing_4q_tonnes": 950.0, "baseline_tonnes": 500.0},
    "dollar": {"reserve_share_pct": 58.0},
    "real_rate": {
        "supportive_below_pct": 1.0,
        "restrictive_above_pct": 2.0,
        "assumed_pct": 1.9,
    },
    "scoring": {
        "weights": {"central_bank": 0.35, "real_rate": 0.30, "dollar": 0.20, "fx": 0.15}
    },
    "risk_off": False,
    "position": {"grams": 2.0, "avg_cost_krw_per_g": None},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Return base deep-merged with override (override wins on leaves)."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: pathlib.Path) -> dict:
    """Load the macro-factors yaml, filling any missing keys from DEFAULT_CONFIG.

    A missing file yields DEFAULT_CONFIG unchanged.
    """
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    loaded = yaml.safe_load(path.read_text()) or {}
    return _deep_merge(DEFAULT_CONFIG, loaded)
