# Implement MetricsAggregator with EWMA and CUSUM

**Role:** backend
**Priority:** 2 (normal)
**Scope:** medium
**Complexity:** high

## Problem
Current AnalysisEngine uses z-score for anomaly detection. Research shows CUSUM
(Cumulative Sum) outperforms z-score for small samples (n=15-20), and EWMA
(Exponential Weighted Moving Average, lambda=0.2) is best for real-time control charts.

Need: Bayesian Online Changepoint Detection (BOCPD, Adams & MacKay) every 20 records
for detecting distribution shifts from model updates.

## Implementation
- EWMA with lambda=0.2 for real-time trend monitoring
- CUSUM for shift detection (better than z-score for small n)
- BOCPD every 20 records for changepoint detection
- Mann-Kendall test for trend significance (valid from n=8)
- Rolling Beta-Binomial posteriors for pass/fail metrics
- Rolling Normal-Inverse-Gamma posteriors for continuous metrics
- Minimum sample sizes: 30 runs for alerting, 50 for A/B, 200+ for trends

## Goodhart's Law defenses (built into aggregator)
- Multi-metric composite scoring (weights hidden from agents)
- Metric divergence detection: any metric improving while correlated metric declines
- Trip wire monitoring: track if agents exploit test loopholes
- Never expose evaluation criteria to the evolution loop

## Files
- src/bernstein/evolution/aggregator.py (new)
- tests/unit/test_aggregator.py (new)

## Completion signals
- path_exists: src/bernstein/evolution/aggregator.py
- test_passes: uv run pytest tests/unit/test_aggregator.py -x -q
- file_contains: src/bernstein/evolution/aggregator.py :: EWMA
- file_contains: src/bernstein/evolution/aggregator.py :: CUSUM
