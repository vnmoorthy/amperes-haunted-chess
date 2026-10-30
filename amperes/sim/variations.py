"""Controlled, reproducible condition generation.

Splits:
- nominal: the fixed baseline configuration (repeated to measure determinism).
- dev: perturbed conditions used for failure discovery and intervention *selection*.
- heldout: perturbed conditions from a disjoint seed space, used only for the final comparison.
- neighborhood: local variations around one failed condition (one-at-a-time sweeps + joint jitter).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

from amperes.sim.env import EnvParams

GENERATOR_VERSION = "1.0.0"

# Sampling ranges are a subset of the validated EnvParams bounds.
PERTURBATION_SPACE: dict[str, tuple[float, float, str]] = {
    "object_x": (-0.06, 0.06, "object start x offset (m)"),
    "object_y": (-0.16, -0.08, "object start y (m)"),
    "object_yaw": (-0.7, 0.7, "object yaw (rad)"),
    "object_half_size": (0.016, 0.028, "object half size (m)"),
    "object_mass": (0.05, 0.9, "object mass (kg)"),
    "object_friction": (0.2, 1.2, "object friction coefficient"),
    "target_x": (-0.08, 0.08, "target x (m)"),
    "target_y": (0.11, 0.18, "target y (m)"),
    "obstacle_height": (0.0, 0.15, "obstacle height (m)"),
    "obs_noise_std": (0.0, 0.02, "object-position estimate noise std (m)"),
}

DEV_MASTER_SEED = 1_000
HELDOUT_MASTER_SEED = 2_000_000


def condition_id(params: dict[str, Any], seed: int) -> str:
    blob = json.dumps({"p": params, "s": seed}, sort_keys=True)
    return "c_" + hashlib.sha256(blob.encode()).hexdigest()[:12]


def make_condition(changed: dict[str, float], seed: int, split: str, parent_id: str | None,
                   description: str, base: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(base or EnvParams().model_dump())
    params.update({k: round(float(v), 5) for k, v in changed.items()})
    EnvParams(**params)  # validates bounds; raises on out-of-range parameters
    return {
        "condition_id": condition_id(params, seed),
        "parent_id": parent_id,
        "split": split,
        "changed": {k: round(float(v), 5) for k, v in changed.items()},
        "env_params": params,
        "seed": int(seed),
        "description": description,
        "generator_version": GENERATOR_VERSION,
    }


def nominal(repeats: int = 3) -> list[dict[str, Any]]:
    return [make_condition({}, seed=i, split="nominal", parent_id=None, description="nominal baseline configuration")
            for i in range(repeats)]


def sample(n: int, split: str, master_seed: int, max_factors: int = 3) -> list[dict[str, Any]]:
    """Each condition perturbs 1..max_factors parameters (uniform within PERTURBATION_SPACE)."""
    rng = np.random.default_rng(master_seed)
    keys = list(PERTURBATION_SPACE)
    out = []
    for i in range(n):
        k = int(rng.integers(1, max_factors + 1))
        chosen = sorted(rng.choice(keys, size=k, replace=False).tolist())
        changed = {c: float(rng.uniform(*PERTURBATION_SPACE[c][:2])) for c in chosen}
        seed = master_seed + i
        desc = ", ".join(f"{c}={changed[c]:.3g}" for c in chosen)
        out.append(make_condition(changed, seed=seed, split=split, parent_id="nominal", description=f"perturbed: {desc}"))
    return out


def dev_set(n: int) -> list[dict[str, Any]]:
    return sample(n, "dev", DEV_MASTER_SEED)


def heldout_set(n: int) -> list[dict[str, Any]]:
    return sample(n, "heldout", HELDOUT_MASTER_SEED)


def assert_disjoint(a: list[dict], b: list[dict]) -> None:
    ida, idb = {c["condition_id"] for c in a}, {c["condition_id"] for c in b}
    seeds_a, seeds_b = {c["seed"] for c in a}, {c["seed"] for c in b}
    if ida & idb or seeds_a & seeds_b:
        raise RuntimeError("held-out conditions overlap with development conditions")


def neighborhood(cond: dict[str, Any], points: int = 7, jitter: int = 12, frac: float = 0.25,
                 seed: int = 7) -> list[dict[str, Any]]:
    """One-at-a-time sweeps over each changed parameter, plus joint jitter around the failed condition."""
    base = cond["env_params"]
    keys = list(cond.get("changed") or {}) or ["object_mass", "object_friction"]
    out = []
    for k in keys:
        lo, hi, _ = PERTURBATION_SPACE[k]
        span = (hi - lo) * frac
        for v in np.linspace(max(lo, base[k] - span), min(hi, base[k] + span), points):
            out.append(make_condition({k: float(v)}, seed=cond["seed"], split="neighborhood",
                                      parent_id=cond["condition_id"], base=base,
                                      description=f"sweep {k}={v:.4g} around {cond['condition_id']}"))
    rng = np.random.default_rng(seed)
    for j in range(jitter):
        changed = {}
        for k in keys:
            lo, hi, _ = PERTURBATION_SPACE[k]
            changed[k] = float(np.clip(base[k] + rng.normal(0, (hi - lo) * 0.08), lo, hi))
        out.append(make_condition(changed, seed=cond["seed"] + 10_000 + j, split="neighborhood",
                                  parent_id=cond["condition_id"], base=base,
                                  description="joint jitter: " + ", ".join(f"{k}={v:.4g}" for k, v in changed.items())))
    return out
