"""Tests for bernstein.cli.crt_effects."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from bernstein.cli.crt_effects import (
    CRTConfig,
    _power_off_frames,
    _power_on_frames,
    apply_scanlines,
    power_off_effect,
    power_on_effect,
)


class _FakeBuffer:
    def __init__(self) -> None:
        self.frames: list[str] = []

    def render_frame(self, frame: str) -> None:
        self.frames.append(frame)

    def __enter__(self) -> _FakeBuffer:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_apply_scanlines_dims_every_other_line() -> None:
    rendered = apply_scanlines("a\nb\nc")

    lines = rendered.splitlines()
    assert lines[0].startswith("\033[2m")
    assert lines[1] == "b"
    assert lines[2].startswith("\033[2m")


def test_power_on_frames_expand_and_show_brand() -> None:
    frames = _power_on_frames(20, 8)

    assert len(frames) >= 2
    assert "BERNSTEIN" in frames[-1]


def test_power_off_frames_end_blank_and_include_dot() -> None:
    frames = _power_off_frames(20, 8, "hello\nworld")

    assert any("•" in frame for frame in frames)
    assert frames[-1].strip() == ""


def test_power_on_effect_renders_final_frame_to_buffer(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("bernstein.cli.crt_effects._key_pressed", lambda: False)
    buffer = _FakeBuffer()

    power_on_effect("done", frame_buffer=buffer, config=CRTConfig(width=20, height=8, fps=120))

    assert buffer.frames
    assert buffer.frames[-1] == "done"


def test_power_off_effect_renders_frames(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("bernstein.cli.crt_effects._key_pressed", lambda: False)
    buffer = _FakeBuffer()

    power_off_effect("bye", frame_buffer=buffer, config=CRTConfig(width=20, height=8, fps=120, power_off_ms=1))

    assert len(buffer.frames) >= 2
