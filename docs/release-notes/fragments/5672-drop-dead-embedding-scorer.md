## Remove dead embedding-scorer references in ContextCompressor

`ContextCompressor` in `src/bernstein/core/tokens/context_compression.py` imported
`EmbeddingScorer` from `bernstein.core.embedding_scorer`, which was previously
removed as dead code. The dead import, unreachable Phase 1 scoring branch, and
`use_embeddings` parameter have been removed, and the class docstring updated
to reflect the actual BM25 and dependency graph selection pipeline (#5672).
