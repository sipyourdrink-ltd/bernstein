"""Drafting helper for adapter capability profiles.

This module contains the logic to draft an adapter capability profile from
probe evidence (captured --help output). The drafting function is used
during onboarding to create a profile that matches the evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bernstein.adapters.capability_profile import InvocationSpec


@dataclass(frozen=True)
class Draft:
    """Drafted adapter capability profile.

    Attributes:
        invocation: The always-passed CLI surface drafted from evidence.
        evidence_byte_range: The (start, end) byte range of the model flag
            in the evidence help text, or None if no model flag was found.
    """

    invocation: InvocationSpec
    evidence_byte_range: tuple[int, int] | None = None


def _find_flag_in_help(help_text: str, flag: str) -> tuple[str, int, int] | None:
    """Find a flag in help text and return (flag, start, end) if found.

    Args:
        help_text: The --help output to search.
        flag: The flag to look for (e.g., "--model" or "-m").

    Returns:
        Tuple of (flag, start_index, end_index) if found, else None.
    """
    # Look for the flag as a whole word (not part of another word)
    pattern = rf"(?<!\w){re.escape(flag)}(?!\w)"
    match = re.search(pattern, help_text)
    if match:
        return flag, match.start(), match.end()
    return None


def draft_from_evidence(
    evidence_path: Any,
    *,
    required_fields: set[str] | None = None,
) -> Draft:
    """Draft an InvocationSpec from probe evidence.

    Args:
        evidence_path: Path to a JSON evidence file produced by _write_evidence_file.
        required_fields: Set of field names that must be present in the evidence.
            If a required field is missing, raises an exception whose message
            contains the missing field name.

    Returns:
        A Draft containing the invocation spec and evidence byte range for the
        model flag (if found).

    Raises:
        Exception: If a required field is missing from the evidence. The
            exception message will contain the missing field name.
    """
    # Load evidence
    if hasattr(evidence_path, "read_text"):
        evidence_json = evidence_path.read_text(encoding="utf-8")
    else:
        evidence_json = Path(evidence_path).read_text(encoding="utf-8")
    evidence = json.loads(evidence_json)

    binary = evidence["binary"]
    help_text = evidence["output"]

    # Initialize invocation spec fields
    model_flag = None
    model_flag_range: tuple[int, int] | None = None
    prompt_flag = None

    # Find model flag: look for --model or -m
    model_flag_match = _find_flag_in_help(help_text, "--model")
    if model_flag_match is None:
        model_flag_match = _find_flag_in_help(help_text, "-m")
    if model_flag_match:
        model_flag, start, end = model_flag_match
        model_flag_range = (start, end)

    # Find prompt flag: look for --prompt or -p
    prompt_flag_match = _find_flag_in_help(help_text, "--prompt")
    if prompt_flag_match is None:
        prompt_flag_match = _find_flag_in_help(help_text, "-p")
    if prompt_flag_match:
        prompt_flag, _, _ = prompt_flag_match

    # Check required fields
    if required_fields:
        missing = []
        if "model_flag" in required_fields and model_flag is None:
            missing.append("--model")
        if "prompt_flag" in required_fields and prompt_flag is None:
            missing.append("--prompt")
        if missing:
            raise ValueError(f"missing required field(s): {', '.join(missing)}")

    # Build invocation spec
    invocation = InvocationSpec(
        binary=binary,
        model_flag=model_flag,
        prompt_flag=prompt_flag,
        prompt_positional=(prompt_flag is None),
        extra_args=(),
        env_passthrough=(),
    )

    return Draft(invocation=invocation, evidence_byte_range=model_flag_range)
