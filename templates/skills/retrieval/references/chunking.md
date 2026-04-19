# Chunking

## Size
- 200-400 tokens for dense retrieval over prose.
- 500-800 tokens for code or structured data.
- Too small: over-retrieves, rerank has nothing to work with.
- Too large: dilutes relevance, exhausts context budget downstream.

## Overlap
- 10-20% overlap between consecutive chunks.
- Larger overlap for long-form narrative; smaller for reference material.

## Boundaries
- Respect semantic boundaries: paragraph > sentence > hard-break > token.
- Never split mid-code-block.
- For Markdown, prefer heading-anchored chunks so each is self-contained.

## Metadata
- Store `source_path`, `source_sha`, `chunk_index`, `heading_path`.
- Keep original offsets so reranking / highlighting can reconstruct the
  source region.
- Re-ingest when source files change; stale chunks poison recall.
