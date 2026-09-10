#!/usr/bin/env python3
"""Pure evaluation helpers for leakage-safe football research.

The primitives in this module intentionally know nothing about production activation.
They provide deterministic, fold-stratified week-block bootstrap confidence intervals
and simple hierarchical shrinkage for small subgroup summaries.
"""
from __future__ import annotations

import math
import random
import zlib
from collections import defaultdict
from statistics import mean
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DEFAULT_BOOTSTRAP_ITERATIONS = 2000
DEFAULT_BOOTSTRAP_SEED = 20260910
DEFAULT_PRIOR_STRENGTH = 30.0
CONFIDENCE_BANDS: Tuple[Tuple[float, float], ...] = (
    (0.50, 0.60),
    (0.60, 0.65),
    (0.65, 0.70),
    (0.70, 0.75),
    (0.75, 1.001),
)


def finite_number(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def confidence_band(value: Any) -> str:
    x = finite_number(value)
    if x is None:
        return "unknown"
    for lo, hi in CONFIDENCE_BANDS:
        if lo <= x < hi:
            return f"{lo:.2f}-{min(1.0, hi):.2f}"
    if x < CONFIDENCE_BANDS[0][0]:
        return f"<{CONFIDENCE_BANDS[0][0]:.2f}"
    return ">=1.00"


def stable_seed(label: str, base_seed: int = DEFAULT_BOOTSTRAP_SEED) -> int:
    return int(base_seed) ^ zlib.crc32(str(label).encode("utf-8"))


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    xs = sorted(float(x) for x in values if math.isfinite(float(x)))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    q = min(1.0, max(0.0, float(q)))
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def stratified_week_sample(
    rows: Sequence[Mapping[str, Any]], *, rng: random.Random
) -> List[Mapping[str, Any]]:
    """Resample weeks with replacement *within each fold* and then concatenate.

    Every original fold contributes the same number of sampled week blocks as it had
    observed week blocks. Rows from one week always move together, so selections that
    share a fixture week are not treated as independent picks.
    """
    by_fold_week: Dict[str, Dict[str, List[Mapping[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        fold = str(row.get("fold", "__single_fold__"))
        week = str(row.get("week", "__single_week__"))
        by_fold_week[fold][week].append(row)
    sampled: List[Mapping[str, Any]] = []
    for fold in sorted(by_fold_week):
        blocks = by_fold_week[fold]
        week_keys = sorted(blocks)
        if not week_keys:
            continue
        for _ in range(len(week_keys)):
            sampled.extend(blocks[rng.choice(week_keys)])
    return sampled


def week_block_bootstrap(
    rows: Sequence[Mapping[str, Any]],
    metric_fn: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Optional[float]]],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> Dict[str, Optional[List[float]]]:
    """Fold-stratified week-block bootstrap CIs for arbitrary scalar metrics."""
    if not rows or iterations <= 0:
        return {}
    rng = random.Random(int(seed))
    draws: Dict[str, List[float]] = defaultdict(list)
    for _ in range(int(iterations)):
        sample = stratified_week_sample(rows, rng=rng)
        metrics = metric_fn(sample)
        for key, value in metrics.items():
            x = finite_number(value)
            if x is not None:
                draws[str(key)].append(x)
    out: Dict[str, Optional[List[float]]] = {}
    for key, vals in draws.items():
        lo = percentile(vals, 0.025)
        hi = percentile(vals, 0.975)
        out[key] = None if lo is None or hi is None else [round(lo, 6), round(hi, 6)]
    return out


def brier(probability: Any, outcome: Any) -> Optional[float]:
    p = finite_number(probability)
    if p is None or outcome is None:
        return None
    y = 1.0 if bool(outcome) else 0.0
    return (p - y) ** 2


def unit_profit(price: Any, won: Any) -> Optional[float]:
    px = finite_number(price)
    if px is None or px <= 1.0 or won is None:
        return None
    return (px - 1.0) if bool(won) else -1.0


def selection_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Optional[float]]:
    """Scalar metrics used by both point estimates and bootstrap draws."""
    out: Dict[str, Optional[float]] = {
        "hit_rate": None,
        "model_brier": None,
        "market_brier": None,
        "delta_model_minus_market": None,
        "roi": None,
    }
    if not rows:
        return out
    hit_values = [1.0 if bool(r.get("hit")) else 0.0 for r in rows if r.get("hit") is not None]
    if hit_values:
        out["hit_rate"] = mean(hit_values)

    model_losses = [brier(r.get("p_selected"), r.get("y_selected")) for r in rows]
    model_losses = [x for x in model_losses if x is not None]
    if model_losses:
        out["model_brier"] = mean(model_losses)

    paired_model: List[float] = []
    paired_market: List[float] = []
    for r in rows:
        ml = brier(r.get("p_selected"), r.get("y_selected"))
        ql = brier(r.get("market_selected_p"), r.get("y_selected"))
        if ml is not None and ql is not None:
            paired_model.append(ml)
            paired_market.append(ql)
    if paired_model:
        out["market_brier"] = mean(paired_market)
        out["delta_model_minus_market"] = mean(a - b for a, b in zip(paired_model, paired_market))

    profits = [unit_profit(r.get("execution_price"), r.get("hit")) for r in rows]
    profits = [x for x in profits if x is not None]
    if profits:
        out["roi"] = mean(profits)
    return out


def paired_brier_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Optional[float]]:
    model_losses: List[float] = []
    market_losses: List[float] = []
    for r in rows:
        y = r.get("y")
        ml = brier(r.get("model_p"), y)
        ql = brier(r.get("market_p"), y)
        if ml is None or ql is None:
            continue
        model_losses.append(ml)
        market_losses.append(ql)
    if not model_losses:
        return {
            "model_brier": None,
            "market_brier": None,
            "delta_model_minus_market": None,
            "brier_skill_vs_market": None,
        }
    mb = mean(model_losses)
    qb = mean(market_losses)
    delta = mb - qb
    skill = (1.0 - mb / qb) if qb > 0 else None
    return {
        "model_brier": mb,
        "market_brier": qb,
        "delta_model_minus_market": delta,
        "brier_skill_vs_market": skill,
    }


def shrink_rate(hits: int, n: int, parent_rate: Optional[float], prior_strength: float = DEFAULT_PRIOR_STRENGTH) -> Optional[float]:
    if n <= 0:
        return parent_rate
    parent = finite_number(parent_rate)
    if parent is None:
        return hits / n
    k = max(0.0, float(prior_strength))
    return (float(hits) + parent * k) / (float(n) + k)


def shrink_mean(raw_mean: Optional[float], n: int, parent_mean: Optional[float], prior_strength: float = DEFAULT_PRIOR_STRENGTH) -> Optional[float]:
    raw = finite_number(raw_mean)
    parent = finite_number(parent_mean)
    if raw is None:
        return parent
    if parent is None or n <= 0:
        return raw
    k = max(0.0, float(prior_strength))
    return (raw * float(n) + parent * k) / (float(n) + k)


def evidence_level(n: int, ci: Optional[Sequence[float]] = None) -> str:
    if n < 15:
        return "insufficient"
    if n < 30:
        return "weak"
    if n < 60:
        return "moderate"
    if ci and len(ci) == 2:
        try:
            if float(ci[1]) - float(ci[0]) > 0.18:
                return "moderate"
        except Exception:
            pass
    return "stronger"
