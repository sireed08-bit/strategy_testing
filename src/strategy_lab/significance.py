"""
Bootstrap significance testing for strategy trade records.

With tens of thousands of parameter combinations tested against one price
path, the best few scores are guaranteed to look good by luck alone. This
module asks the question scoring cannot: could this strategy's average trade
plausibly be zero-edge noise?

Method: percentile bootstrap on the per-trade net returns.
  - p-value: recentre the trades to zero mean (impose the null hypothesis of
    no edge), resample with replacement many times, and count how often a
    no-edge world produces a mean at least as good as the observed one.
  - Confidence interval: resample the ORIGINAL trades and take the 2.5th and
    97.5th percentiles of the resampled means.

A p-value near 0.5 means "indistinguishable from luck". And remember the
multiple-testing reality: with ~30k experiments, even p=0.01 findings appear
~300 times by chance — treat this as a filter, not a proof.
"""
from __future__ import annotations

import random
from statistics import mean


def bootstrap_trade_significance(
    trades: list[float],
    resamples: int = 2000,
    rng: random.Random | None = None,
) -> dict:
    if len(trades) < 5:
        return {"status": "insufficient_trades", "n_trades": len(trades)}

    rng = rng or random.Random(1234)
    observed = mean(trades)
    n = len(trades)

    # Null world: same trade-size distribution, zero true edge.
    centered = [t - observed for t in trades]
    at_least_as_good = 0
    for _ in range(resamples):
        if mean(rng.choices(centered, k=n)) >= observed:
            at_least_as_good += 1
    p_value = at_least_as_good / resamples

    # CI on the real mean trade via uncentered resampling.
    resampled_means = sorted(mean(rng.choices(trades, k=n)) for _ in range(resamples))
    lo = resampled_means[int(0.025 * resamples)]
    hi = resampled_means[int(0.975 * resamples)]

    return {
        "status": "evaluated",
        "n_trades": n,
        "mean_trade_pct": round(observed * 100.0, 3),
        "p_value": round(p_value, 4),
        "ci95_mean_trade_pct": [round(lo * 100.0, 3), round(hi * 100.0, 3)],
        "resamples": resamples,
    }
