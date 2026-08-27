# Analytics & Billing (D1)

Bernstein's Cloudflare integration uses **D1** -- Cloudflare's serverless SQLite -- as the persistence layer for usage analytics and billing-tier enforcement.

> **Prompt caching note.** Bernstein's prompt caching is delivered via Anthropic's native `cache_control` headers (`core/agents/prompt_cache.py`), independent of Cloudflare Vectorize.
