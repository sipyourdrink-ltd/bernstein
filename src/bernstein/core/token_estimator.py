"""File-type-aware token estimation for context budgeting (T803).

Different file types have different token densities. This module provides
a ``bytes_per_token_for_file_type()`` helper that returns a rough estimate
of how many bytes correspond to one token, based on the file extension.

Token density values (bytes/token) from Claude Code's ``tokenEstimation.ts``:
- JSON/data files: ~2 bytes per token (high token density)
- Source code: ~4 bytes per token (medium token density)
- Markdown/text: ~4 bytes per token (medium token density)
- Minified/compressed: ~6 bytes per token (low token density)
- Binary files: not estimable (return ``None``)

Usage:
    >>> bytes_per_token_for_file_type("data.json")
    2.0
    >>> bytes_per_token_for_file_type("server.py")
    4.0
    >>> estimate_tokens_for_file_size("report.md", 8000)
    2000
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Token density constants — bytes per token for different file categories
# ---------------------------------------------------------------------------

#: JSON and data files use ~2 bytes/token (dense key-value packing)
JSON_BYTES_PER_TOKEN: float = 2.0

#: Source code files use ~4 bytes/token (identifiers, whitespace, structure)
CODE_BYTES_PER_TOKEN: float = 4.0

#: Markdown and prose use ~4 bytes/token (similar to code)
TEXT_BYTES_PER_TOKEN: float = 4.0

#: Minified/compressed code uses ~6 bytes/token (very dense, little structure)
MINIFIED_BYTES_PER_TOKEN: float = 6.0

#: Default fallback when file type is unknown
DEFAULT_BYTES_PER_TOKEN: float = 4.0

# ---------------------------------------------------------------------------
# File type classifiers
# ---------------------------------------------------------------------------


_JSON_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".json",
        ".jsonl",
        ".jsonc",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".xml",
    }
)

_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".clj",
        ".cljs",
        ".go",
        ".rs",
        ".c",
        ".cpp",
        ".cc",
        ".h",
        ".hpp",
        ".hxx",
        ".swift",
        ".m",
        ".mm",
        ".rb",
        ".php",
        ".lua",
        ".pl",
        ".pm",
        ".raku",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        ".r",
        ".R",
        ".jl",
        ".cs",
        ".fs",
        ".fsx",
        ".vb",
        ".dart",
        ".ex",
        ".exs",
        ".erl",
        ".hrl",
        ".tf",
        ".hcl",
        ".nix",
        ".zig",
        ".v",
        ".vhd",
        ".vhdl",
        ".sql",
        ".proto",
        ".thrift",
        ".graphql",
        ".gql",
    }
)

_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".md",
        ".rst",
        ".txt",
        ".textile",
        ".asciidoc",
        ".adoc",
        ".log",
        ".csv",
        ".tsv",
    }
)

_MINIFIED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".min.js",
        ".min.css",
    }
)

_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".tiff",
        ".ico",
        ".svg",
        ".webp",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".o",
        ".class",
        ".pyc",
        ".pyo",
        ".egg",
        ".whl",
        ".wasm",
        ".iso",
        ".img",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".flac",
        ".ogg",
        ".wav",
        ".woff",
        ".woff2",
        ".eot",
        ".ttf",
        ".pkl",
        ".pickle",
        ".joblib",
        ".parquet",
        ".arrow",
        ".feather",
    }
)

# Known filenames that don't have extensions but are still analyzable
_SPECIAL_NAMES: frozenset[str] = frozenset(
    {
        "Makefile",
        "Dockerfile",
        "Containerfile",
        "Vagrantfile",
        "Jenkinsfile",
        "LICENSE",
        "README",
        "CHANGELOG",
        "NOTICE",
        ".gitignore",
        ".dockerignore",
        ".env",
        ".env.example",
    }
)


def _is_minified(content_sample: bytes) -> bool:
    """Heuristic: check if content is minified (single extremely long line)."""
    if not content_sample:
        return False
    try:
        lines = content_sample.split(b"\n")
    except Exception:
        return False
    if len(lines) <= 1:
        return True
    # If most characters are on one line, it's likely minified
    max_line = max(len(line) for line in lines)
    return max_line > len(content_sample) * 0.8


def bytes_per_token_for_file_type(file_path: str | Path) -> float | None:
    """Estimate bytes-per-token for a file based on its type.

    Args:
        file_path: Path or filename string to classify.

    Returns:
        Estimated bytes per token, or ``None`` if the file is binary
        and cannot be sensibly estimated.
    """
    path = Path(file_path)
    name = path.name
    stem = path.stem
    suffix = path.suffix.lower()

    # Check for minified files first (stem ends with ".min")
    if stem.endswith(".min") and suffix in {".js", ".css"}:
        return MINIFIED_BYTES_PER_TOKEN

    if suffix in _BINARY_EXTENSIONS:
        return None

    if suffix in _JSON_EXTENSIONS:
        return JSON_BYTES_PER_TOKEN

    if suffix in _CODE_EXTENSIONS:
        return CODE_BYTES_PER_TOKEN

    if suffix in _TEXT_EXTENSIONS:
        return TEXT_BYTES_PER_TOKEN

    if name in _SPECIAL_NAMES:
        return TEXT_BYTES_PER_TOKEN

    # Fallback: if the file exists, try a heuristic based on content
    if path.is_file():
        try:
            snippet = path.read_bytes()[:4096]
            if _is_minified(snippet):
                return MINIFIED_BYTES_PER_TOKEN
            # Treat unknown text files as code
            return CODE_BYTES_PER_TOKEN
        except OSError:
            pass  # File unreadable; use default estimate

    return DEFAULT_BYTES_PER_TOKEN


def estimate_tokens_for_file_size(file_path: str | Path, size_bytes: int) -> int:
    """Estimate the token count for a file given its byte size.

    Args:
        file_path: Path or filename string to classify.
        size_bytes: File size in bytes.

    Returns:
        Estimated token count (rounded down).  Zero for binary files.
    """
    bpt = bytes_per_token_for_file_type(file_path)
    if bpt is None:
        return 0
    if bpt <= 0:
        return 0
    return int(size_bytes / bpt)


def estimate_tokens_for_text(text: str, assumed_type: str = "code") -> int:
    """Estimate token count for a raw text string.

    Args:
        text: The text content to estimate.
        assumed_type: Category to use for estimation. One of
            ``"json"``, ``"code"``, ``"text"``, ``"minified"``.

    Returns:
        Estimated token count (rounded down).
    """
    bytes_map: dict[str, float] = {
        "json": JSON_BYTES_PER_TOKEN,
        "code": CODE_BYTES_PER_TOKEN,
        "text": TEXT_BYTES_PER_TOKEN,
        "minified": MINIFIED_BYTES_PER_TOKEN,
    }
    bpt = bytes_map.get(assumed_type, DEFAULT_BYTES_PER_TOKEN)
    return int(len(text.encode("utf-8")) / bpt)
