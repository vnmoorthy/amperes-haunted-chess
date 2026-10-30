"""Small-sample statistics implemented directly (no scipy): Wilson intervals, exact McNemar, paired bootstrap."""

from __future__ import annotations

import math
from statistics import mean, median
from typing import Any

import numpy as np

Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def mcnemar_exact(b: int, c: int) -> float | None:
    """Two-sided exact McNemar p-value on discordant pairs (b: only A succeeded, c: only B succeeded)."""
    n = b + c
    if n == 0:
        return None
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * p)


def paired_bootstrap_diff(a: list[int], b: list[int], iters: int = 10_000, seed: int = 0) -> tuple[float, float]:
    """95 % percentile CI of mean(b) - mean(a), resampling paired conditions."""
    a_, b_ = np.asarray(a, float), np.asarray(b, float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(a_), size=(iters, len(a_)))
    diffs = b_[idx].mean(axis=1) - a_[idx].mean(axis=1)
    return float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def outcome_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    s = sum(r["outcome"] == "success" for r in rows)
    lo, hi = wilson(s, n)
    durations = [r["sim_duration_s"] for r in rows if r["outcome"] == "success"]
    cats: dict[str, int] = {}
    for r in rows:
        if r.get("category"):
            cats[r["category"]] = cats.get(r["category"], 0) + 1
    return {
        "n": n,
        "successes": s,
        "failures": sum(r["outcome"] == "failure" for r in rows),
        "timeouts": sum(r["outcome"] == "timeout" for r in rows),
        "invalid": sum(r["outcome"] == "invalid" for r in rows),
        "success_rate": s / n if n else None,
        "success_rate_ci95": [lo, hi],
        "failure_rate": (n - s) / n if n else None,
        "collision_rate": sum(bool(r.get("collision")) for r in rows) / n if n else None,
        "timeout_rate": sum(r["outcome"] == "timeout" for r in rows) / n if n else None,
        "mean_duration_successful_s": mean(durations) if durations else None,
        "median_duration_successful_s": median(durations) if durations else None,
        "mean_duration_all_s": mean(r["sim_duration_s"] for r in rows) if rows else None,
        "wall_time_total_s": round(sum(r["wall_time_s"] for r in rows), 3),
        "failure_categories": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
    }


def paired_comparison(base: list[dict[str, Any]], treat: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare two policies on identical condition lists (matched by condition_id)."""
    bm = {r["condition_id"]: r for r in base}
    tm = {r["condition_id"]: r for r in treat}
    keys = [k for k in bm if k in tm]
    if len(keys) != len(bm) or len(keys) != len(tm):
        raise ValueError("paired comparison requires identical condition sets")
    a = [int(bm[k]["outcome"] == "success") for k in keys]
    b = [int(tm[k]["outcome"] == "success") for k in keys]
    only_base = sum(x == 1 and y == 0 for x, y in zip(a, b))
    only_treat = sum(x == 0 and y == 1 for x, y in zip(a, b))
    lo, hi = paired_bootstrap_diff(a, b) if keys else (None, None)
    return {
        "n_pairs": len(keys),
        "baseline": outcome_summary([bm[k] for k in keys]),
        "treatment": outcome_summary([tm[k] for k in keys]),
        "both_success": sum(x == 1 and y == 1 for x, y in zip(a, b)),
        "both_fail": sum(x == 0 and y == 0 for x, y in zip(a, b)),
        "fixed_by_treatment": only_treat,
        "broken_by_treatment": only_base,
        "success_rate_diff": (sum(b) - sum(a)) / len(keys) if keys else None,
        "success_rate_diff_ci95_paired_bootstrap": [lo, hi],
        "mcnemar_exact_p": mcnemar_exact(only_base, only_treat),
        "method": "paired by condition_id; exact two-sided McNemar on discordant pairs; 10k paired bootstrap (seed 0)",
    }
