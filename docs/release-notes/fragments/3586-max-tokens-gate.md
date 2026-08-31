### Changed

- Treat `max_tokens` as part of the enforced per-spawn sampling surface: adapters without coarse sampling support or `SUPPORTS_MAX_TOKENS` now refuse the spawn instead of silently dropping the requested limit. The OpenAI Agents adapter declares and continues to honour the override. ([#3586](https://github.com/sipyourdrink-ltd/bernstein/issues/3586))
