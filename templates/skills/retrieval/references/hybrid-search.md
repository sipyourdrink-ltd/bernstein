# Hybrid search (dense + sparse)

## Why both
- **Dense** catches paraphrase, concept overlap, multilingual query/doc
  drift.
- **Sparse** (BM25 / SPLADE) catches rare tokens, exact phrasing, IDs.
- Hybrid wins on out-of-distribution queries where either alone loses.

## Fusion
- **Reciprocal Rank Fusion** (RRF) is the safe default — no tuning.
- **Weighted linear** only works when scores are calibrated; normalize
  per-query first.
- **Learned-to-rank** pays off at scale but needs training data.

## Pipeline
1. Retrieve top-N from each system (N=100-200).
2. Fuse into a candidate set (top-K, K=25-50).
3. Optional: cross-encoder rerank down to top-k (k=5-10).

## Pitfalls
- BM25 over a tokenizer that disagrees with the embedding model will drift.
- Duplicate docs (same chunk ingested twice) corrupt both recall and
  reranker training.
- Filter pushdown before retrieval, not after — post-filtering discards
  quality candidates.
