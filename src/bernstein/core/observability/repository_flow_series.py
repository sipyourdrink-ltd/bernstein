"""Append-only JSONL series for repository-flow samples.

This module provides persistence for repository-flow observations without
any sampling or decision logic. Slice 2 of #4850 — persistence only.

The format is JSONL with sorted keys, UTF-8, LF terminator. This matches
the house style used by ``decision_log.py``, ``trace_store.py`` and
``abandons.jsonl``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class RepositoryFlowSeriesError(ValueError):
    """Raised when a series file is malformed.

    The message includes the 1-based line number of the offending line.
    """

    pass


@dataclass(frozen=True)
class RepositoryFlowSample:
    """One observation of repository flow state.

    Slice 1 of #4850 was supposed to define the sample type; it has not
    landed yet. Since this module is slice 2 (persistence) and the
    stagnation rule is slice 3, the dataclass only needs the fields that
    make the byte-determinism test meaningful.

    Attributes:
        observed_at: Unix timestamp in seconds.
        commits_per_min: Commits per minute rate.
        open_prs: Count of open pull requests.
        churn_lines: Lines of code churn.
        open_issues: Count of open issues.
    """

    observed_at: float
    commits_per_min: float
    open_prs: int
    churn_lines: int
    open_issues: int


def serialize_sample(sample: RepositoryFlowSample) -> bytes:
    """Serialize a sample to deterministic JSON bytes.

    Uses sorted keys, compact separators, UTF-8 encoding. Same input
    always produces identical bytes across processes.

    Args:
        sample: The sample to serialize.

    Returns:
        UTF-8 encoded JSON bytes (no trailing newline).
    """
    payload = {
        "observed_at": sample.observed_at,
        "commits_per_min": sample.commits_per_min,
        "open_prs": sample.open_prs,
        "churn_lines": sample.churn_lines,
        "open_issues": sample.open_issues,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def deserialize_sample(line: bytes) -> RepositoryFlowSample:
    """Deserialize a JSON line to a RepositoryFlowSample.

    Args:
        line: UTF-8 encoded JSON bytes.

    Returns:
        Parsed RepositoryFlowSample.

    Raises:
        RepositoryFlowSeriesError: If the line is not valid UTF-8,
            not a JSON object, or missing required fields.
    """
    try:
        text = line.decode("utf-8").strip()
    except UnicodeDecodeError as e:
        raise RepositoryFlowSeriesError(f"Non-UTF-8 bytes: {e}") from e

    if not text:
        raise RepositoryFlowSeriesError("Blank line")

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise RepositoryFlowSeriesError(f"Invalid JSON: {e}") from e

    if not isinstance(obj, dict):
        raise RepositoryFlowSeriesError(f"Expected JSON object, got {type(obj).__name__}")

    try:
        return RepositoryFlowSample(
            observed_at=obj["observed_at"],
            commits_per_min=obj["commits_per_min"],
            open_prs=obj["open_prs"],
            churn_lines=obj["churn_lines"],
            open_issues=obj["open_issues"],
        )
    except KeyError as e:
        raise RepositoryFlowSeriesError(f"Missing required field: {e}") from e
    except TypeError as e:
        raise RepositoryFlowSeriesError(f"Wrong type on field: {e}") from e


def append_sample(path: Path, sample: RepositoryFlowSample) -> None:
    """Append a sample to a JSONL series file.

    Opens the file in append mode, writes the serialized sample plus a
    newline, and closes. Creates the file if it does not exist. Does not
    read the file first — the OS append mode guarantees the existing
    bytes remain unchanged.

    Args:
        path: Path to the series file.
        sample: The sample to append.
    """
    data = serialize_sample(sample) + b"\n"
    with open(path, "ab") as f:
        f.write(data)


def read_samples(path: Path) -> list[RepositoryFlowSample]:
    """Read all samples from a JSONL series file.

    Iterates lines and parses each as a RepositoryFlowSample. Raises
    RepositoryFlowSeriesError with the 1-based line number on any
    malformed line. Trailing whitespace is allowed and stripped. The
    file may end with or without a final newline.

    Args:
        path: Path to the series file.

    Returns:
        List of samples in write order.

    Raises:
        RepositoryFlowSeriesError: If any line is malformed. The message
            includes the 1-based line number.
    """
    samples: list[RepositoryFlowSample] = []
    with open(path, "rb") as f:
        for line_num, line in enumerate(f, start=1):
            try:
                sample = deserialize_sample(line)
            except RepositoryFlowSeriesError as e:
                raise RepositoryFlowSeriesError(f"line {line_num}: {e}") from e
            samples.append(sample)
    return samples
