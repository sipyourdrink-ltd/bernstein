# Python conventions (Bernstein)

- Python 3.12+, strict typing (Pyright strict mode) — no `Any`, no untyped dicts.
- Use dataclasses or TypedDict, never raw dict soup.
- Ruff for linting and formatting:
  - `uv run ruff check src/`
  - `uv run ruff format src/`
- Google-style docstrings only where non-obvious.
- Async for IO-bound operations, sync for CPU-bound.
- No `from __future__ import` beyond `annotations` — the project targets 3.12.
- Prefer `Path` over raw strings for filesystems.
- Use `structlog`/stdlib `logging` with lazy formatting (`logger.info("x=%s", x)`).

## Naming
- Private helpers are prefixed with `_`.
- Constants are `UPPER_SNAKE_CASE`.
- Classes: `PascalCase`. Functions / methods: `snake_case`.

## Typing idioms
- `list[int]`, `dict[str, Any]` — no `typing.List`, no `typing.Dict`.
- `X | None` — no `Optional[X]`.
- `TYPE_CHECKING` imports are encouraged to keep runtime cost down.
