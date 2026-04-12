"""Retro CRT power-on and power-off effects for the premium CLI."""

from __future__ import annotations

import select
import sys
import time
from dataclasses import dataclass

from bernstein.cli.frame_buffer import FrameBuffer
from bernstein.cli.visual_theme import CRT_GRADIENT, PALETTE, sample_gradient


@dataclass(frozen=True)
class CRTConfig:
    """Configuration for terminal CRT effects."""

    width: int = 80
    height: int = 24
    fps: int = 30
    power_on_ms: int = 500
    power_off_ms: int = 800
    scanlines: bool = False


def _key_pressed() -> bool:
    """Return True when a key is waiting on stdin."""
    if not sys.stdin.isatty():
        return False
    try:
        return bool(select.select([sys.stdin], [], [], 0.0)[0])
    except (OSError, ValueError):
        return False


def _bg_hex(color: str) -> str:
    normalized = color.lstrip("#")
    r = int(normalized[0:2], 16)
    g = int(normalized[2:4], 16)
    b = int(normalized[4:6], 16)
    return f"\033[48;2;{r};{g};{b}m"


def _fg_hex(color: str) -> str:
    normalized = color.lstrip("#")
    r = int(normalized[0:2], 16)
    g = int(normalized[2:4], 16)
    b = int(normalized[4:6], 16)
    return f"\033[38;2;{r};{g};{b}m"


def apply_scanlines(frame: str) -> str:
    """Apply a subtle dim pass to every other row."""
    if not frame:
        return frame
    lines = frame.splitlines()
    processed: list[str] = []
    for index, line in enumerate(lines):
        if index % 2 == 0 and line:
            processed.append(f"\033[2m{line}\033[0m")
        else:
            processed.append(line)
    return "\n".join(processed)


def _center_label(width: int, text: str, *, fg: str) -> str:
    """Render a centered ANSI-styled text line."""
    visible = text[:width]
    padding = max(0, (width - len(visible)) // 2)
    return " " * padding + f"{_fg_hex(fg)}{visible}\033[0m"


def _power_on_frames(width: int, height: int) -> list[str]:
    """Build frames for a CRT power-on animation."""
    if width <= 0 or height <= 0:
        return []
    frame_count = 8
    colors = sample_gradient(CRT_GRADIENT, frame_count)
    center = height // 2
    frames: list[str] = []
    for step in range(frame_count):
        half_span = max(0, round((step / max(frame_count - 1, 1)) * (height / 2)))
        bg = _bg_hex(colors[step])
        rows: list[str] = []
        for row in range(height):
            if center - half_span <= row <= center + half_span:
                rows.append(f"{bg}{' ' * width}\033[0m")
            else:
                rows.append(" " * width)
        if step >= frame_count // 2:
            rows[center] = _center_label(width, "BERNSTEIN", fg=PALETTE.glow)
        frames.append("\n".join(rows))
    return frames


def _power_off_frames(width: int, height: int, frame: str | None = None) -> list[str]:
    """Build frames for a CRT shutdown animation."""
    if width <= 0 or height <= 0:
        return []
    base_lines = (frame.splitlines() if frame else [])[:height]
    if len(base_lines) < height:
        base_lines.extend([" " * width for _ in range(height - len(base_lines))])
    colors = sample_gradient(tuple(reversed(CRT_GRADIENT)), 10)
    center = height // 2
    frames: list[str] = []
    current = base_lines
    for index, color in enumerate(colors[:-2]):
        keep_rows = max(1, height - round((index + 1) * (height / max(len(colors) - 2, 1))))
        start = max(0, center - keep_rows // 2)
        end = min(height, start + keep_rows)
        rows = [" " * width for _ in range(height)]
        for row_idx in range(start, end):
            rows[row_idx] = current[row_idx]
        if keep_rows <= 2:
            rows[center] = f"{_bg_hex(color)}{' ' * width}\033[0m"
        frames.append("\n".join(rows))
    dot_pad = max(0, (width // 2) - 1)
    line_frame = [" " * width for _ in range(height)]
    line_frame[center] = f"{_bg_hex(PALETTE.glow)}{' ' * width}\033[0m"
    frames.append("\n".join(line_frame))
    dot_frame = [" " * width for _ in range(height)]
    dot_frame[center] = " " * dot_pad + f"{_fg_hex(PALETTE.glow)}•\033[0m"
    frames.append("\n".join(dot_frame))
    frames.append("\n".join([" " * width for _ in range(height)]))
    return frames


def power_on_effect(
    final_frame: str | None = None,
    *,
    frame_buffer: FrameBuffer | None = None,
    config: CRTConfig | None = None,
) -> None:
    """Render the CRT power-on animation to stdout."""
    cfg = config or CRTConfig()
    frames = _power_on_frames(cfg.width, cfg.height)
    if cfg.scanlines:
        frames = [apply_scanlines(frame) for frame in frames]
    buffer = frame_buffer or FrameBuffer(fps=cfg.fps)
    with buffer:
        for frame in frames:
            buffer.render_frame(frame)
            if _key_pressed():
                break
        if final_frame:
            buffer.render_frame(apply_scanlines(final_frame) if cfg.scanlines else final_frame)


def power_off_effect(
    frame: str | None = None,
    *,
    frame_buffer: FrameBuffer | None = None,
    config: CRTConfig | None = None,
) -> None:
    """Render the CRT power-off animation to stdout."""
    cfg = config or CRTConfig()
    frames = _power_off_frames(cfg.width, cfg.height, frame)
    if cfg.scanlines:
        frames = [apply_scanlines(current) for current in frames]
    buffer = frame_buffer or FrameBuffer(fps=cfg.fps)
    with buffer:
        for current in frames:
            buffer.render_frame(current)
            if _key_pressed():
                buffer.render_frame("\n".join([" " * cfg.width for _ in range(cfg.height)]))
                break
            time.sleep(cfg.power_off_ms / max(len(frames), 1) / 1000.0)
