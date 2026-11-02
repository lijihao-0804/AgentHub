"""Small-sample statistics for agent evaluation (AE-2).

The evaluation platform already reports point estimates.  A point estimate from a few
dozen cases cannot support a release decision, so this module adds the four things a
claim needs: a reliability view (``pass^k``), an interval, a paired test, and the
minimum detectable effect at the sample size actually used.

No third-party dependency is introduced: the Jeffreys interval is computed from the
regularized incomplete beta function implemented below, and bootstrap resampling uses
``random.Random`` with an explicit seed so a result can be reproduced.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import mean, pvariance
from typing import Any

DEFAULT_BOOTSTRAP_COUNT = 2000
DEFAULT_SEED = 20261102
DEFAULT_ALPHA = 0.05
DEFAULT_POWER = 0.8


@dataclass(frozen=True)
class TrialOutcome:
    """One trial of one case.  ``success=None`` means the trial is unknown."""

    case_key: str
    trial_index: int
    success: bool | None
    output_hash: str | None = None


def summarize_reliability(
    outcomes: Sequence[TrialOutcome], *, k: int
) -> dict[str, Any]:
    """Return ``pass^k``, variance and consistency for a run's trials.

    A case is *complete* only when it has ``k`` known trials.  Incomplete cases stay in
    ``incomplete_case_count`` and are excluded from the ``pass^k`` denominator: an
    unknown trial can never be counted as a success, and dropping the case silently
    would overstate reliability.
    """

    if k < 1:
        raise ValueError("k must be positive")

    grouped: dict[str, list[TrialOutcome]] = defaultdict(list)
    for outcome in outcomes:
        grouped[outcome.case_key].append(outcome)

    known_trials = [item for item in outcomes if item.success is not None]
    successes = sum(1 for item in known_trials if item.success)
    complete: list[list[TrialOutcome]] = []
    incomplete = 0
    for trials in grouped.values():
        known = [item for item in trials if item.success is not None]
        if len(known) == k:
            complete.append(known)
        else:
            incomplete += 1

    complete_success = [all(item.success for item in trials) for trials in complete]
    pass_hat_k = mean(complete_success) if complete_success else None
    variance = (
        mean(pvariance([float(bool(item.success)) for item in trials]) for trials in complete)
        if complete
        else None
    )
    with_hashes = [trials for trials in complete if all(item.output_hash for item in trials)]
    consistency = (
        mean(
            (
                1.0
                if all(item.success for item in trials)
                and len({item.output_hash for item in trials}) == 1
                else 0.0
            )
            for trials in with_hashes
        )
        if with_hashes
        else None
    )

    return {
        "k": k,
        "case_count": len(grouped),
        "complete_case_count": len(complete),
        "incomplete_case_count": incomplete,
        "trial_count": len(outcomes),
        "known_trial_count": len(known_trials),
        "unknown_trial_count": len(outcomes) - len(known_trials),
        "success_count": successes,
        "success_rate": successes / len(known_trials) if known_trials else None,
        "success_coverage_rate": len(known_trials) / len(outcomes) if outcomes else None,
        "complete_case_rate": len(complete) / len(grouped) if grouped else None,
        # Complete-case reliability.  Keep the conservative variant next to it so a run
        # with many unknown trials cannot look reliable just by dropping them.
        "pass_hat_k": pass_hat_k,
        "pass_hat_k_all_cases": (
            sum(complete_success) / len(grouped) if grouped else None
        ),
        "trial_variance": variance,
        "consistency_at_k": consistency,
        "literal_consistency_at_k": consistency,
        "case_success_rates": {
            case_key: mean(float(bool(item.success)) for item in trials if item.success is not None)
            for case_key, trials in grouped.items()
            if any(item.success is not None for item in trials)
        },
        "case_all_success": {
            trials[0].case_key: all(bool(item.success) for item in trials)
            for trials in complete
        },
    }


def jeffreys_interval(
    successes: int, total: int, *, alpha: float = DEFAULT_ALPHA
) -> tuple[float, float] | None:
    """Equal-tailed Jeffreys interval for a binomial proportion.

    ``Beta(successes + 0.5, failures + 0.5)``.  This is the small-sample default: with
    fewer than a few hundred cases a CLT interval understates uncertainty.
    """

    if total <= 0:
        return None
    if not 0 <= successes <= total:
        raise ValueError("successes must be between 0 and total")
    a = successes + 0.5
    b = total - successes + 0.5
    return (_beta_quantile(alpha / 2, a, b), _beta_quantile(1 - alpha / 2, a, b))


def cluster_bootstrap_interval(
    values: Sequence[float],
    *,
    statistic: Callable[[Sequence[float]], float] = mean,
    bootstrap_count: int = DEFAULT_BOOTSTRAP_COUNT,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
) -> tuple[float, float] | None:
    """Percentile interval from case-level resampling (not trial-level)."""

    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    estimates = [
        statistic([values[rng.randrange(n)] for _ in range(n)])
        for _ in range(bootstrap_count)
    ]
    return _percentile(estimates, alpha / 2), _percentile(estimates, 1 - alpha / 2)


def paired_bootstrap_interval(
    pairs: Sequence[tuple[float, float]],
    *,
    bootstrap_count: int = DEFAULT_BOOTSTRAP_COUNT,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
) -> tuple[float, float] | None:
    """Percentile interval for ``candidate - baseline`` over paired cases."""

    if not pairs:
        return None
    deltas = [candidate - baseline for baseline, candidate in pairs]
    return cluster_bootstrap_interval(
        deltas,
        bootstrap_count=bootstrap_count,
        seed=seed,
        alpha=alpha,
    )


def mcnemar_exact(baseline_only: int, candidate_only: int) -> float:
    """Two-sided exact McNemar p-value for paired binary outcomes."""

    if baseline_only < 0 or candidate_only < 0:
        raise ValueError("discordant counts must be non-negative")
    n = baseline_only + candidate_only
    if n == 0:
        return 1.0
    if n > 1000:
        z = (abs(baseline_only - candidate_only) - 1) / math.sqrt(n)
        return min(1.0, 2 * (1 - _normal_cdf(max(0.0, z))))
    smaller = min(baseline_only, candidate_only)
    tail = sum(math.comb(n, i) for i in range(smaller + 1)) / (2**n)
    return min(1.0, 2 * tail)


def holm_adjust(p_values: Sequence[tuple[str, float]]) -> dict[str, float]:
    """Holm–Bonferroni adjusted p-values, keyed by metric name."""

    if not p_values:
        return {}
    ordered = sorted(p_values, key=lambda item: item[1])
    count = len(ordered)
    adjusted: dict[str, float] = {}
    running = 0.0
    for index, (name, p_value) in enumerate(ordered):
        running = max(running, min(1.0, p_value * (count - index)))
        adjusted[name] = running
    return adjusted


def required_pairs(
    delta: float,
    discordance: float,
    *,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> int | None:
    """Approximate number of paired cases needed to detect ``delta``.

    ``discordance`` is ``p10 + p01``: the share of cases where the two variants disagree.
    A difference of ``delta`` implies ``p10 = (discordance + delta) / 2`` and
    ``p01 = (discordance - delta) / 2``.
    """

    if delta <= 0 or discordance <= 0 or delta >= discordance:
        return None
    z_alpha = _normal_quantile(1 - alpha / 2)
    z_power = _normal_quantile(power)
    numerator = (
        z_alpha * math.sqrt(discordance)
        + z_power * math.sqrt(max(0.0, discordance - delta * delta))
    ) ** 2
    return math.ceil(numerator / (delta * delta))


def minimum_detectable_effect(
    pairs: int,
    discordance: float,
    *,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> float | None:
    """Smallest ``delta`` detectable with ``pairs`` paired cases.

    Solved by bisection over :func:`required_pairs`; returns ``None`` when the sample
    cannot detect any positive difference under the assumed discordance.
    """

    if pairs <= 0 or discordance <= 0:
        return None
    low, high = 1e-6, min(discordance - 1e-9, 0.999)
    if required_pairs(high, discordance, alpha=alpha, power=power) is None:
        return None
    if required_pairs(high, discordance, alpha=alpha, power=power) > pairs:
        return None
    for _ in range(80):
        middle = (low + high) / 2
        needed = required_pairs(middle, discordance, alpha=alpha, power=power)
        if needed is None or needed > pairs:
            # This delta needs more pairs than we have: it is too small to be feasible.
            low = middle
        else:
            high = middle
    return high


def statistical_summary(
    outcomes: Sequence[TrialOutcome],
    *,
    k: int,
    discordance: float | None = None,
) -> dict[str, Any]:
    """Reliability + interval + MDE in one payload for the statistics endpoint."""

    summary = summarize_reliability(outcomes, k=k)
    known = summary["known_trial_count"]
    successes = summary["success_count"]
    interval = jeffreys_interval(successes, known)
    summary["success_rate_interval"] = (
        {
            "method": "jeffreys",
            "basis": "known_trials",
            "lower": interval[0],
            "upper": interval[1],
        }
        if interval
        else None
    )
    summary["reliability_note"] = (
        "pass_hat_k uses complete cases only; pass_hat_k_all_cases counts every unknown "
        "trial as a failure.  Report complete_case_rate before claiming reliability."
    )
    summary["mde"] = (
        {
            "pairs": summary["complete_case_count"],
            "assumed_discordance": discordance,
            "delta": minimum_detectable_effect(summary["complete_case_count"], discordance)
            if discordance is not None
            else None,
        }
        if discordance is not None
        else None
    )
    return summary


def _percentile(values: Sequence[float], rank: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("empty values")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * rank
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _normal_quantile(p: float) -> float:
    # Acklam's rational approximation is accurate to ~1e-9 and avoids a scipy import.
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (
            ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        ) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(
            ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        ) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
    ) / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


def _beta_quantile(p: float, a: float, b: float) -> float:
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    low, high = 0.0, 1.0
    for _ in range(200):
        middle = (low + high) / 2
        if _regularized_beta(a, b, middle) < p:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _regularized_beta(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    log_beta = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    beta = math.exp(log_beta)
    if x < (a + 1) / (a + b + 2):
        return beta * _beta_continued_fraction(a, b, x) / a
    return 1 - beta * _beta_continued_fraction(b, a, 1 - x) / b


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    max_iterations = 300
    epsilon = 3e-14
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c = 1.0
    d = 1 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1 / d
    h = d
    for m in range(1, max_iterations + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < epsilon:
            break
    return h


__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_BOOTSTRAP_COUNT",
    "DEFAULT_POWER",
    "DEFAULT_SEED",
    "TrialOutcome",
    "cluster_bootstrap_interval",
    "holm_adjust",
    "jeffreys_interval",
    "mcnemar_exact",
    "minimum_detectable_effect",
    "paired_bootstrap_interval",
    "required_pairs",
    "statistical_summary",
    "summarize_reliability",
]
