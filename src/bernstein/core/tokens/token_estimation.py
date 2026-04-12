"""File-type-aware token estimation helpers.

Estimate LLM token counts from file size or text content using different
bytes-per-token ratios depending on the file type.

Rationale:
    Tokenizers handle different content categories with varying efficiency.
    Structured data (JSON, YAML) packs more tokens per byte than source code,
    which in turn packs more than prose.  Using a single constant (e.g., 4
    bytes/token) over-estimates tokens for JSON and under-estimates for dense
    code.

Reference values (from empirical tokenizer measurements):

- JSON / JSONL / JSONC / YAML / TOML: ~2 bytes/token (dense, structured)
- Source code (Python, JS, TS, Rust, Go, etc.): ~4 bytes/token
- Text / Markdown / XML / HTML: ~3 bytes/token
- Minified JS / CSS: ~1 byte/token (extreme density)
- Binary (PNG, PDF, ZIP, etc.): ~1 byte/token (not useful for LLM context)
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Constants — bytes per token by file category
# ---------------------------------------------------------------------------

#: Structured data formats: dense key-value pairs.
JSON_BYTES_PER_TOKEN: float = 2.0

#: Source code files: identifiers, whitespace, comments.
CODE_BYTES_PER_TOKEN: float = 4.0

#: Prose, documentation, and markup: natural language density.
TEXT_BYTES_PER_TOKEN: float = 3.0

#: Minified bundles: extreme density, no whitespace.
MINIFIED_BYTES_PER_TOKEN: float = 1.0

#: Fallback for unknown file extensions.
DEFAULT_BYTES_PER_TOKEN: float = 3.5

# Binary extensions — no meaningful token representation for LLM context.
_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".bmp",
        ".ico",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".pkl",
        ".pickle",
        ".bin",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".a",
        ".o",
        ".pyc",
        ".pyo",
        ".pyd",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".class",
        ".jar",
        ".war",
        ".ear",
        ".iso",
        ".dmg",
        ".wasm",
    }
)

#: Extensions that always use JSON_BYTES_PER_TOKEN.
_JSON_EXTENSIONS: frozenset[str] = frozenset({".json", ".jsonl", ".jsonc", ".yaml", ".yml", ".toml"})

#: Extensions that always use CODE_BYTES_PER_TOKEN.
_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".sc",
        ".rb",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".cxx",
        ".cs",
        ".swift",
        ".m",
        ".mm",
        ".r",
        ".pl",
        ".pm",
        ".php",
        ".dart",
        ".clj",
        ".cljs",
        ".ex",
        ".exs",
        ".erl",
        ".hrl",
        ".hs",
        ".lua",
        ".ml",
        ".mli",
        ".nim",
        ".zig",
        ".v",
        ".vhdl",
        ".sql",
        ".proto",
        ".tf",
        ".hcl",
        ".cmake",
    }
)

#: Minified file patterns (base name ends with ``.min.<ext>``).
_MIN_PATTERNS: frozenset[str] = frozenset({".min.js", ".min.css", ".min.ts"})

#: Text/prose extensions.
_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".md",
        ".txt",
        ".rst",
        ".org",
        ".adoc",
        ".asciidoc",
        ".textile",
        ".log",
        ".csv",
        ".tsv",
    }
)

#: Markup extensions.
_MARKUP_EXTENSIONS: frozenset[str] = frozenset({".html", ".htm", ".xml", ".xhtml", ".svg"})

#: Special filenames with no extension (e.g., Makefile, Dockerfile).
_SPECIAL_NAMES: frozenset[str] = frozenset(
    {
        "Makefile",
        "Dockerfile",
        "Vagrantfile",
        "Gemfile",
        "Rakefile",
        "Procfile",
        "CMakeLists.txt",
    }
)

# -----------------------------------------------------------------------
# Internal lookup table for quick resolution
# -----------------------------------------------------------------------

_CATEGORY_BPT: dict[str, float] = {
    "json": JSON_BYTES_PER_TOKEN,
    "code": CODE_BYTES_PER_TOKEN,
    "text": TEXT_BYTES_PER_TOKEN,
    "markup": TEXT_BYTES_PER_TOKEN,
    "minified": MINIFIED_BYTES_PER_TOKEN,
    "binary": 0.0,
    "default": DEFAULT_BYTES_PER_TOKEN,
}


# -----------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------


def bytes_per_token_for_file_type(file_path: str | Path) -> float | None:
    """Return the estimated bytes-per-token ratio for a given file.

    Inspects the file extension (or full name for extension-less special
    files like ``Makefile``) and returns the appropriate ratio.  Binary
    files return ``None`` to signal that token estimation is not meaningful.

    Args:
        file_path: File path as a string or ``pathlib.Path`` object.

    Returns:
        Bytes per token estimate, or ``None`` for binary files that are not
        useful as LLM context.
    """
    name = Path(file_path).name
    lower = name.lower()

    # Special filenames (no extension, well-known names).
    if lower in _SPECIAL_NAMES and "." not in lower:
        return TEXT_BYTES_PER_TOKEN

    # Dotfiles with no extension (e.g., .gitignore).
    if lower.startswith(".") and "." not in lower[1:]:
        return TEXT_BYTES_PER_TOKEN

    # Minified files first (they have code extensions but different density).
    for pattern in _MIN_PATTERNS:
        if lower.endswith(pattern):
            return MINIFIED_BYTES_PER_TOKEN

    ext = Path(file_path).suffix.lower()

    if ext in _BINARY_EXTENSIONS:
        return None
    if ext in _JSON_EXTENSIONS:
        return JSON_BYTES_PER_TOKEN
    if ext in _CODE_EXTENSIONS:
        return CODE_BYTES_PER_TOKEN
    if ext in _TEXT_EXTENSIONS:
        return TEXT_BYTES_PER_TOKEN
    if ext in _MARKUP_EXTENSIONS:
        return TEXT_BYTES_PER_TOKEN

    return DEFAULT_BYTES_PER_TOKEN


def estimate_tokens_for_file_size(file_path: str | Path, size_bytes: int) -> int:
    """Estimate token count from file size and type.

    Returns ``0`` for binary files (not suitable for LLM context).

    Args:
        file_path: File path as a string or ``pathlib.Path``.
        size_bytes: Size of the file in bytes.

    Returns:
        Estimated token count (integer, floor division).
    """
    if size_bytes == 0:
        return 0

    bpt = bytes_per_token_for_file_type(file_path)
    if bpt is None or abs(bpt) < 1e-9:
        return 0
    return int(size_bytes / bpt)


def estimate_tokens_for_text(text: str, assumed_type: str = "code") -> int:
    """Estimate token count for a text string.

    Uses the UTF-8 byte length of the text and the bytes-per-token ratio for
    the given ``assumed_type``.

    Args:
        text: The text content to estimate.
        assumed_type: One of ``"code"``, ``"json"``, ``"text"``, ``"markup"``,
            or ``"default"``.  Falls back to :data:`DEFAULT_BYTES_PER_TOKEN`
            for unknown types.

    Returns:
        Estimated token count (integer, floor division).
    """
    bpt = _CATEGORY_BPT.get(assumed_type, DEFAULT_BYTES_PER_TOKEN)
    if abs(bpt) < 1e-9:
        return 0
    byte_len = len(text.encode("utf-8"))
    return int(byte_len / bpt)


def estimate_tokens_for_file(file_path: str | Path, content: bytes | str) -> int:
    """Estimate token count for actual file content by type.

    Combines :func:`bytes_per_token_for_file_type` with the real byte length
    of *content*.  Binary files (where no meaningful ratio exists) return ``0``.

    Args:
        file_path: File path as a string or ``pathlib.Path`` (used for type
            detection only).
        content: Raw file content as ``bytes`` or decoded ``str``.

    Returns:
        Estimated token count (integer, floor division).
    """
    bpt = bytes_per_token_for_file_type(file_path)
    if bpt is None or abs(bpt) < 1e-9:
        return 0
    size_bytes = len(content) if isinstance(content, bytes) else len(content.encode("utf-8"))
    return int(size_bytes / bpt)
