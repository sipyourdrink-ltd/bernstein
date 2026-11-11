# Analytics & Billing (D1)

Bernstein's Cloudflare integration is intended to use **D1** -- Cloudflare's serverless SQLite -- as the persistence layer for usage analytics and billing-tier enforcement. No D1 client, metering, or billing-tier code exists in the current source tree; this page is a placeholder.

> **Prompt caching note.** Bernstein's prompt caching is delivered via Anthropic's native `cache_control` headers (`core/agents/prompt_cache.py`), independent of Cloudflare Vectorize.
