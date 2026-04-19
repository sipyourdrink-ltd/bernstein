# ML evaluation

## Metric selection
- Classification: accuracy only when classes are balanced. Prefer F1,
  precision-at-K, ROC-AUC otherwise.
- Regression: MAE / RMSE; add R² only to compare models on the same data.
- Retrieval: recall@K, MRR, nDCG.
- Generation: human eval > automatic eval. BLEU/ROUGE are weak signals for
  modern LLMs.

## Protocol
- Split data before anything else; never tune on the test set.
- Fix the random seed for every component (data, model init, dropout).
- Use stratified splits when class imbalance is > 10:1.
- Compare against at least one baseline (random / majority / last-known-good).

## Statistical rigour
- Report confidence intervals, not just point estimates.
- Use bootstrapping for small test sets.
- A 0.1 point improvement is probably noise unless you have 1k+ samples.
