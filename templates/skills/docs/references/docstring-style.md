# Docstring style (Google)

- Only where non-obvious — a typed, well-named helper does not need a
  docstring that restates its signature.
- Google style:

```python
def fetch(url: str, *, timeout: float = 5.0) -> Response:
    """Fetch the URL and return the parsed response.

    Args:
        url: Absolute http(s) URL.
        timeout: Per-request timeout in seconds.

    Returns:
        Parsed ``Response``.

    Raises:
        TimeoutError: When the server did not answer within ``timeout``.
    """
```

- Imperative mood ("Fetch the URL…"), not descriptive ("Fetches…").
- Document raised exceptions and keyword-only arguments.
- For async functions, note the awaitable contract only when non-obvious.
- Use ``Example:`` sections when the call site needs a pattern, not when
  the signature is self-evident.

## README / markdown
- One ``H1`` per file.
- Sentence-case headings.
- Fenced code blocks always declare their language.
- Links over footnotes; absolute URLs for external references.
