"""Voice command layer — speech-to-text via whisper.cpp for hands-free control.

Implements ``bernstein listen``: captures microphone audio, transcribes locally
using faster-whisper (offline, no cloud), parses utterances into CLI commands,
and either prints the parsed command (``--dry-run``) or executes it.

Custom voice aliases are loaded from ``~/.bernstein/voice.yaml``.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import console

# ---------------------------------------------------------------------------
# Default voice.yaml location
# ---------------------------------------------------------------------------

_DEFAULT_ALIAS_FILE: Path = Path.home() / ".bernstein" / "voice.yaml"

# ---------------------------------------------------------------------------
# Number-word → int mapping (for "run three agents on …")
# ---------------------------------------------------------------------------

_WORD_TO_INT: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _parse_workers(raw: str) -> str:
    """Convert a worker count token (digit string or English word) to a digit string.

    Args:
        raw: The raw matched token, e.g. ``"3"`` or ``"three"``.

    Returns:
        A digit string such as ``"3"``.
    """
    if raw.isdigit():
        return raw
    return str(_WORD_TO_INT.get(raw.lower(), raw))


# ---------------------------------------------------------------------------
# Command grammar — (pattern, cli_args_template)
# Patterns are tried in order; first match wins.
# Named groups from the regex are available for template substitution.
# ---------------------------------------------------------------------------

_WORD_NUMBERS = "|".join(_WORD_TO_INT.keys())
_WORKERS_PAT = rf"(?P<workers>\d+|{_WORD_NUMBERS})"

_GRAMMAR: list[tuple[re.Pattern[str], str]] = [
    # "run N agents on <goal>"  /  "run agents on <goal>"
    (
        re.compile(
            rf"\brun\s+(?:{_WORKERS_PAT}\s+)?agents?\s+on\s+(?P<goal>.+)",
            re.IGNORECASE,
        ),
        "{base} -g {goal!r}{workers_flag}",
    ),
    # "run <goal>"  (no "agents" keyword)
    (
        re.compile(r"\brun\s+(?P<goal>.+)", re.IGNORECASE),
        "{base} -g {goal!r}",
    ),
    # "status" / "show status"
    (
        re.compile(r"\b(?:show\s+)?status\b", re.IGNORECASE),
        "{base} status",
    ),
    # "stop" / "stop agents" / "stop all"
    (
        re.compile(r"\bstop\b", re.IGNORECASE),
        "{base} stop",
    ),
    # "list agents" / "show agents"
    (
        re.compile(r"\b(?:list|show)\s+agents?\b", re.IGNORECASE),
        "{base} agents list",
    ),
    # "show results" / "show recap" / "recap"
    (
        re.compile(r"\b(?:show\s+)?(?:results?|recap)\b", re.IGNORECASE),
        "{base} recap",
    ),
    # "show logs" / "logs"
    (
        re.compile(r"\b(?:show\s+)?logs?\b", re.IGNORECASE),
        "{base} logs",
    ),
    # "show cost" / "cost"
    (
        re.compile(r"\b(?:show\s+)?cost\b", re.IGNORECASE),
        "{base} cost",
    ),
    # "plan" / "show plan"
    (
        re.compile(r"\b(?:show\s+)?plan\b", re.IGNORECASE),
        "{base} plan",
    ),
    # "help"
    (
        re.compile(r"\bhelp\b", re.IGNORECASE),
        "{base} --help",
    ),
]

# ---------------------------------------------------------------------------
# Alias loading
# ---------------------------------------------------------------------------


def _load_aliases(alias_file: Path) -> dict[str, str]:
    """Load custom voice command aliases from a YAML file.

    The file maps utterance phrases (lowercased) to CLI command strings, e.g.::

        deploy prod: bernstein -g "deploy to production"
        check tests: bernstein -g "run full test suite"

    Args:
        alias_file: Path to the YAML alias file.

    Returns:
        Dict mapping utterance phrases to CLI command strings.
        Empty dict if the file does not exist or cannot be parsed.
    """
    if not alias_file.exists():
        return {}
    try:
        import yaml  # already a project dep (bernstein.yaml parsing)

        with open(alias_file) as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            return {}
        return {str(k).lower().strip(): str(v) for k, v in data.items()}
    except Exception as exc:
        console.print(f"[yellow]Warning: could not load voice aliases from {alias_file}: {exc}[/yellow]")
        return {}


# ---------------------------------------------------------------------------
# Utterance → CLI command parsing
# ---------------------------------------------------------------------------


def _base_command() -> str:
    """Return the command prefix used when building CLI strings.

    Returns:
        The ``bernstein`` entry-point invocation string.
    """
    return f"{sys.executable} -m bernstein"


def parse_utterance(text: str, aliases: dict[str, str]) -> str | None:
    """Map a transcribed utterance to a CLI command string.

    Alias lookup is tried first (exact, lowercased). Then the built-in grammar
    patterns are tried in order. Returns *None* if nothing matches.

    Args:
        text: Raw transcribed text from the speech-to-text engine.
        aliases: Custom alias mapping loaded from voice.yaml.

    Returns:
        A CLI command string ready to pass to ``subprocess.run``, or *None*.
    """
    cleaned = text.strip()
    lower = cleaned.lower()

    # 1. Alias lookup (exact match on lowercased input)
    if lower in aliases:
        return aliases[lower]

    # 2. Partial alias prefix match (alias key appears at start of utterance)
    for phrase, cmd in aliases.items():
        if lower.startswith(phrase):
            return cmd

    # 3. Built-in grammar
    base = _base_command()
    for pattern, template in _GRAMMAR:
        m = pattern.search(cleaned)
        if m is None:
            continue
        groups = m.groupdict()
        goal = groups.get("goal", "").strip() if groups.get("goal") else ""
        raw_workers = groups.get("workers", "").strip() if groups.get("workers") else ""
        workers = _parse_workers(raw_workers) if raw_workers else ""
        workers_flag = f" -j {workers}" if workers else ""
        try:
            cmd = template.format(
                base=base,
                goal=goal,
                workers_flag=workers_flag,
            )
        except KeyError:
            cmd = template.format(base=base)
        return cmd

    return None


# ---------------------------------------------------------------------------
# Audio feedback
# ---------------------------------------------------------------------------


def _beep() -> None:
    """Emit a terminal bell character for audio feedback."""
    sys.stdout.write("\a")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Transcription helpers
# ---------------------------------------------------------------------------


def _transcribe_chunk(model: Any, audio: Any) -> str:
    """Transcribe a numpy audio array using the faster-whisper model.

    Args:
        model: A loaded ``faster_whisper.WhisperModel`` instance.
        audio: 1-D float32 numpy array at 16 000 Hz.

    Returns:
        Concatenated transcription text (stripped).
    """
    segments, _ = model.transcribe(audio, language="en", beam_size=1)
    return " ".join(s.text for s in segments).strip()


def _load_whisper_model(model_size: str) -> Any:
    """Load and return a faster-whisper model.

    Args:
        model_size: One of ``tiny``, ``base``, ``small``, ``medium``, ``large``.

    Returns:
        A ``faster_whisper.WhisperModel`` instance.

    Raises:
        SystemExit: If faster-whisper is not installed.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        console.print(
            "[red]Error:[/red] 'faster-whisper' is not installed.\n"
            "Install it with: [bold]pip install faster-whisper[/bold]"
        )
        raise SystemExit(1) from exc

    console.print(f"[dim]Loading whisper model '{model_size}' (first run may download ~150 MB)…[/dim]")
    return WhisperModel(model_size, device="cpu", compute_type="int8")


def _capture_and_transcribe(
    model: Any,
    silence_threshold: float,
    min_audio_secs: float,
    sample_rate: int = 16_000,
) -> str | None:
    """Record a single voice utterance and return its transcription.

    Waits for audio above *silence_threshold*, records until silence returns,
    then transcribes. Returns *None* on keyboard interrupt or empty audio.

    Args:
        model: Loaded faster-whisper model.
        silence_threshold: RMS amplitude to distinguish speech from silence.
        min_audio_secs: Minimum seconds of audio required before transcribing.
        sample_rate: Audio sample rate in Hz (must match whisper's 16 kHz).

    Returns:
        Transcribed text, or *None* if no speech was captured.
    """
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        missing = "sounddevice" if "sounddevice" in str(exc) else "numpy"
        console.print(
            f"[red]Error:[/red] '{missing}' is not installed.\n"
            f"Install it with: [bold]pip install sounddevice numpy[/bold]"
        )
        raise SystemExit(1) from exc

    chunk_secs = 0.3
    chunk_samples = int(sample_rate * chunk_secs)

    audio_frames: list[Any] = []
    recording = False
    silence_chunks = 0
    _MAX_SILENCE_CHUNKS = int(1.5 / chunk_secs)  # 1.5 s of silence ends utterance

    try:
        with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32") as stream:
            while True:
                data, _ = stream.read(chunk_samples)
                rms = float(np.sqrt(np.mean(data**2)))

                if rms > silence_threshold:
                    if not recording:
                        recording = True
                        console.print("[cyan]Listening…[/cyan]", end="\r")
                    audio_frames.append(data.copy())
                    silence_chunks = 0
                elif recording:
                    silence_chunks += 1
                    audio_frames.append(data.copy())
                    if silence_chunks >= _MAX_SILENCE_CHUNKS:
                        break

    except KeyboardInterrupt:
        return None

    if not audio_frames:
        return None

    audio_np = np.concatenate(audio_frames, axis=0).flatten()
    duration = len(audio_np) / sample_rate
    if duration < min_audio_secs:
        return None

    return _transcribe_chunk(model, audio_np)


# ---------------------------------------------------------------------------
# Main listen loop
# ---------------------------------------------------------------------------


def _listen_loop(
    model: Any,
    aliases: dict[str, str],
    dry_run: bool,
    silence_threshold: float,
    min_audio_secs: float,
) -> None:
    """Continuously listen, transcribe, and dispatch voice commands.

    Args:
        model: Loaded faster-whisper model.
        aliases: Custom alias mapping.
        dry_run: If True, print the parsed command but do not execute.
        silence_threshold: RMS threshold for speech detection.
        min_audio_secs: Minimum utterance duration to transcribe.
    """
    console.print("[bold green]Voice listener active[/bold green]  [dim](Ctrl+C to stop)[/dim]")
    if dry_run:
        console.print("[yellow]--dry-run mode: commands will be shown but not executed.[/yellow]")

    while True:
        console.print("[dim]Waiting for voice input…[/dim]", end="\r")
        text = _capture_and_transcribe(model, silence_threshold, min_audio_secs)

        if text is None:
            # KeyboardInterrupt or too short — stop cleanly
            break

        if not text:
            continue

        console.print(f"\n[bold]Heard:[/bold] {text}")

        cmd = parse_utterance(text, aliases)
        if cmd is None:
            console.print("[dim]  → No matching command. Try: run/status/stop/list agents/recap.[/dim]")
            continue

        _beep()
        console.print(f"[green]  → Command:[/green] [bold]{cmd}[/bold]")

        if dry_run:
            console.print("[dim]  (dry-run: not executing)[/dim]")
            continue

        try:
            # SECURITY: use list args, not shell=True — cmd contains voice input
            subprocess.run(shlex.split(cmd), check=False)
        except Exception as exc:
            console.print(f"[red]  Error executing command:[/red] {exc}")

        _beep()


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("listen")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Show parsed command without executing.",
)
@click.option(
    "--model",
    "model_size",
    default="base",
    show_default=True,
    type=click.Choice(["tiny", "base", "small", "medium", "large-v2"], case_sensitive=False),
    help="Whisper model size. Smaller = faster but less accurate.",
)
@click.option(
    "--alias-file",
    "alias_file",
    default=str(_DEFAULT_ALIAS_FILE),
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Path to voice alias YAML file.",
)
@click.option(
    "--threshold",
    "silence_threshold",
    default=0.01,
    show_default=True,
    type=float,
    help="RMS amplitude threshold to distinguish speech from silence.",
)
@click.option(
    "--min-duration",
    "min_audio_secs",
    default=0.5,
    show_default=True,
    type=float,
    help="Minimum utterance duration in seconds before transcription.",
)
def listen_cmd(
    dry_run: bool,
    model_size: str,
    alias_file: str,
    silence_threshold: float,
    min_audio_secs: float,
) -> None:
    """Start a voice command session (speech-to-text via whisper, fully offline).

    \b
    Listens to the default microphone, transcribes speech locally using
    faster-whisper, and maps recognized utterances to Bernstein CLI commands.

    \b
    Supported phrases (examples):
      "run three agents on the auth refactor"  → bernstein -g "the auth refactor" -j 3
      "status"                                 → bernstein status
      "stop"                                   → bernstein stop
      "list agents"                            → bernstein agents list
      "show recap"                             → bernstein recap

    \b
    Custom aliases can be defined in ~/.bernstein/voice.yaml:
      deploy prod: bernstein -g "deploy to production"

    \b
    Requirements (install separately):
      pip install faster-whisper sounddevice numpy

    \b
      bernstein listen                   # start voice session
      bernstein listen --dry-run         # preview commands without executing
      bernstein listen --model tiny      # use the smallest/fastest model
    """
    aliases = _load_aliases(Path(alias_file))
    if aliases:
        console.print(f"[dim]Loaded {len(aliases)} voice alias(es) from {alias_file}[/dim]")

    model = _load_whisper_model(model_size)

    try:
        _listen_loop(
            model=model,
            aliases=aliases,
            dry_run=dry_run,
            silence_threshold=silence_threshold,
            min_audio_secs=min_audio_secs,
        )
    except SystemExit:
        raise
    except KeyboardInterrupt:
        pass
    finally:
        console.print("\n[dim]Voice listener stopped.[/dim]")
