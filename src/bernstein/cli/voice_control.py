"""Voice control intent parsing for hands-free orchestration (road-039).

Defines the intent model and pattern-matching logic that maps speech-to-text
transcripts into actionable CLI intents.  This is the model/parsing layer only
-- actual microphone capture and STT backends are wired separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Intent patterns — (regex, action) pairs evaluated in order
# ---------------------------------------------------------------------------

INTENT_PATTERNS: list[tuple[str, str]] = [
    (r"run.*plan", "run"),
    (r"stop.*(?:all|agents?)", "stop"),
    (r"(?:show|what).*status", "status"),
    (r"(?:how much|cost|spent)", "cost"),
    (r"help", "help"),
]

# Pre-compiled for performance.
_COMPILED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pat, re.IGNORECASE), action) for pat, action in INTENT_PATTERNS
]

# Pattern used to extract a plan file reference from a transcript.
_PLAN_REF_RE: re.Pattern[str] = re.compile(
    r"(?:run|execute|start)\s+(?:the\s+)?(\w+)\s+plan",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceIntent:
    """Parsed intent from a voice transcript.

    Attributes:
        action: The resolved CLI action.
        plan_file: Optional plan file name extracted from the transcript.
        target: Optional target entity (agent id, stage name, etc.).
        confidence: Confidence score for the match (0.0 -- 1.0).
    """

    action: Literal["run", "stop", "status", "cost", "help", "unknown"]
    plan_file: str | None = None
    target: str | None = None
    confidence: float = 0.0


@dataclass(frozen=True)
class VoiceConfig:
    """Configuration for the voice control subsystem.

    Attributes:
        enabled: Whether voice control is active.
        confirmation_required: Prompt user before executing parsed intents.
        wake_word: Wake word that activates listening.
    """

    enabled: bool = False
    confirmation_required: bool = True
    wake_word: str = "bernstein"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def extract_plan_reference(transcript: str) -> str | None:
    """Extract a plan file name from a voice transcript.

    Looks for phrases like "run the auth plan" and returns the plan name
    (e.g. ``"auth"``).  Returns ``None`` if no plan reference is found.

    Args:
        transcript: Raw transcript text from speech-to-text.

    Returns:
        The extracted plan name, or ``None``.
    """
    m = _PLAN_REF_RE.search(transcript)
    if m:
        return m.group(1).lower()
    return None


def parse_voice_intent(transcript: str) -> VoiceIntent:
    """Match a transcript against known intent patterns.

    Evaluates ``INTENT_PATTERNS`` in order and returns the first match.  If
    no pattern matches, returns an intent with action ``"unknown"`` and zero
    confidence.

    Args:
        transcript: Raw transcript text from speech-to-text.

    Returns:
        A :class:`VoiceIntent` with the best-matching action.
    """
    text = transcript.strip()
    if not text:
        return VoiceIntent(action="unknown", confidence=0.0)

    for compiled, action in _COMPILED_PATTERNS:
        m = compiled.search(text)
        if m:
            # Confidence is proportional to how much of the transcript the
            # match covers, floored at 0.5 for any match.
            span_ratio = (m.end() - m.start()) / len(text)
            confidence = round(max(0.5, min(1.0, 0.5 + span_ratio)), 2)

            plan_file = extract_plan_reference(text) if action == "run" else None

            return VoiceIntent(
                action=action,  # type: ignore[arg-type]
                plan_file=plan_file,
                confidence=confidence,
            )

    return VoiceIntent(action="unknown", confidence=0.0)


# ---------------------------------------------------------------------------
# Confirmation formatting
# ---------------------------------------------------------------------------


def format_confirmation(intent: VoiceIntent) -> str:
    """Format a human-readable confirmation prompt for a parsed intent.

    Args:
        intent: The parsed voice intent to confirm.

    Returns:
        A string like ``"I understood: run plan 'auth'. Proceed? [Y/n]"``.
    """
    parts: list[str] = [f"I understood: {intent.action}"]
    if intent.plan_file:
        parts.append(f"plan '{intent.plan_file}'")
    if intent.target:
        parts.append(f"target '{intent.target}'")
    return " ".join(parts) + ". Proceed? [Y/n]"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_voice_config(yaml_path: Path | None = None) -> VoiceConfig:
    """Load voice control configuration from a YAML file.

    If *yaml_path* is ``None`` or the file does not exist, returns default
    configuration (voice disabled, confirmation required, wake word
    ``"bernstein"``).

    Args:
        yaml_path: Path to a YAML config file.  May contain keys
            ``enabled``, ``confirmation_required``, and ``wake_word``.

    Returns:
        A :class:`VoiceConfig` instance.
    """
    if yaml_path is None or not yaml_path.exists():
        return VoiceConfig()

    try:
        import yaml
    except ModuleNotFoundError:  # pragma: no cover
        return VoiceConfig()

    raw: Any = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return VoiceConfig()

    voice_section: dict[str, Any] = raw.get("voice", raw)
    return VoiceConfig(
        enabled=bool(voice_section.get("enabled", False)),
        confirmation_required=bool(voice_section.get("confirmation_required", True)),
        wake_word=str(voice_section.get("wake_word", "bernstein")),
    )
