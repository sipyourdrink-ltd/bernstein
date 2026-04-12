"""Tests for multi-modal agent support (#692)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from bernstein.core.agents.multimodal import (
    ModalityType,
    MultiModalContext,
    MultiModalInput,
    build_multimodal_context,
    detect_modality,
    encode_input,
    is_multimodal_capable,
)

# ---------------------------------------------------------------------------
# ModalityType
# ---------------------------------------------------------------------------


class TestModalityType:
    def test_values(self) -> None:
        assert ModalityType.TEXT == "text"
        assert ModalityType.IMAGE == "image"
        assert ModalityType.DIAGRAM == "diagram"
        assert ModalityType.AUDIO == "audio"

    def test_is_str(self) -> None:
        for m in ModalityType:
            assert isinstance(m, str)


# ---------------------------------------------------------------------------
# MultiModalInput
# ---------------------------------------------------------------------------


class TestMultiModalInput:
    def test_frozen(self) -> None:
        inp = MultiModalInput(modality=ModalityType.TEXT)
        with pytest.raises(AttributeError):
            inp.modality = ModalityType.IMAGE  # type: ignore[misc]

    def test_defaults(self) -> None:
        inp = MultiModalInput(modality=ModalityType.IMAGE)
        assert inp.content_path is None
        assert inp.content_base64 is None
        assert inp.mime_type == "application/octet-stream"
        assert inp.description == ""

    def test_with_values(self) -> None:
        p = Path("/tmp/test.png")
        inp = MultiModalInput(
            modality=ModalityType.IMAGE,
            content_path=p,
            content_base64="abc123",
            mime_type="image/png",
            description="screenshot",
        )
        assert inp.content_path == p
        assert inp.content_base64 == "abc123"
        assert inp.mime_type == "image/png"
        assert inp.description == "screenshot"


# ---------------------------------------------------------------------------
# MultiModalContext
# ---------------------------------------------------------------------------


class TestMultiModalContext:
    def test_frozen(self) -> None:
        ctx = MultiModalContext(inputs=(), primary_modality=ModalityType.TEXT)
        with pytest.raises(AttributeError):
            ctx.primary_modality = ModalityType.IMAGE  # type: ignore[misc]

    def test_defaults(self) -> None:
        ctx = MultiModalContext(inputs=())
        assert ctx.primary_modality == ModalityType.TEXT
        assert ctx.inputs == ()


# ---------------------------------------------------------------------------
# detect_modality
# ---------------------------------------------------------------------------


class TestDetectModality:
    @pytest.mark.parametrize(
        ("ext", "expected"),
        [
            (".png", ModalityType.IMAGE),
            (".jpg", ModalityType.IMAGE),
            (".jpeg", ModalityType.IMAGE),
            (".gif", ModalityType.IMAGE),
            (".bmp", ModalityType.IMAGE),
            (".webp", ModalityType.IMAGE),
            (".svg", ModalityType.IMAGE),
            (".tiff", ModalityType.IMAGE),
            (".ico", ModalityType.IMAGE),
        ],
    )
    def test_image_extensions(self, ext: str, expected: ModalityType) -> None:
        assert detect_modality(f"/tmp/file{ext}") == expected

    @pytest.mark.parametrize(
        ("ext", "expected"),
        [
            (".mmd", ModalityType.DIAGRAM),
            (".dot", ModalityType.DIAGRAM),
            (".puml", ModalityType.DIAGRAM),
            (".drawio", ModalityType.DIAGRAM),
            (".d2", ModalityType.DIAGRAM),
        ],
    )
    def test_diagram_extensions(self, ext: str, expected: ModalityType) -> None:
        assert detect_modality(f"/tmp/file{ext}") == expected

    @pytest.mark.parametrize(
        ("ext", "expected"),
        [
            (".mp3", ModalityType.AUDIO),
            (".wav", ModalityType.AUDIO),
            (".ogg", ModalityType.AUDIO),
            (".flac", ModalityType.AUDIO),
            (".m4a", ModalityType.AUDIO),
        ],
    )
    def test_audio_extensions(self, ext: str, expected: ModalityType) -> None:
        assert detect_modality(f"/tmp/file{ext}") == expected

    @pytest.mark.parametrize(
        "ext",
        [".py", ".ts", ".js", ".rs", ".go", ".java", ".md", ".txt", ".yaml", ".json"],
    )
    def test_text_fallback(self, ext: str) -> None:
        assert detect_modality(f"/tmp/file{ext}") == ModalityType.TEXT

    def test_case_insensitive(self) -> None:
        assert detect_modality("/tmp/file.PNG") == ModalityType.IMAGE
        assert detect_modality("/tmp/file.Jpg") == ModalityType.IMAGE
        assert detect_modality("/tmp/file.MMD") == ModalityType.DIAGRAM

    def test_accepts_path_object(self) -> None:
        assert detect_modality(Path("/tmp/file.png")) == ModalityType.IMAGE

    def test_no_extension(self) -> None:
        assert detect_modality("/tmp/Makefile") == ModalityType.TEXT


# ---------------------------------------------------------------------------
# encode_input
# ---------------------------------------------------------------------------


class TestEncodeInput:
    def test_encodes_file(self, tmp_path: Path) -> None:
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        result = encode_input(f)

        assert result.modality == ModalityType.TEXT
        assert result.content_path == f
        assert result.content_base64 is not None
        decoded = base64.b64decode(result.content_base64).decode("utf-8")
        assert decoded == "hello world"
        assert result.description == "hello.txt"

    def test_detects_mime_type_png(self, tmp_path: Path) -> None:
        f = tmp_path / "shot.png"
        f.write_bytes(b"\x89PNG\r\n")
        result = encode_input(f)

        assert result.modality == ModalityType.IMAGE
        assert result.mime_type == "image/png"

    def test_detects_mime_type_jpg(self, tmp_path: Path) -> None:
        f = tmp_path / "photo.jpg"
        f.write_bytes(b"\xff\xd8\xff")
        result = encode_input(f)

        assert result.modality == ModalityType.IMAGE
        assert result.mime_type == "image/jpeg"

    def test_unknown_extension_falls_back(self, tmp_path: Path) -> None:
        f = tmp_path / "data.xyzzy"
        f.write_bytes(b"\x00\x01\x02")
        result = encode_input(f)

        assert result.mime_type == "application/octet-stream"

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="File not found"):
            encode_input("/nonexistent/file.png")

    def test_binary_roundtrip(self, tmp_path: Path) -> None:
        payload = bytes(range(256))
        f = tmp_path / "binary.bin"
        f.write_bytes(payload)
        result = encode_input(f)

        assert result.content_base64 is not None
        assert base64.b64decode(result.content_base64) == payload


# ---------------------------------------------------------------------------
# build_multimodal_context
# ---------------------------------------------------------------------------


class TestBuildMultimodalContext:
    def test_empty_list(self) -> None:
        ctx = build_multimodal_context([])
        assert ctx.inputs == ()
        assert ctx.primary_modality == ModalityType.TEXT

    def test_single_text_file(self, tmp_path: Path) -> None:
        f = tmp_path / "main.py"
        f.write_text("print('hi')")
        ctx = build_multimodal_context([f])

        assert len(ctx.inputs) == 1
        assert ctx.primary_modality == ModalityType.TEXT

    def test_single_image_file(self, tmp_path: Path) -> None:
        f = tmp_path / "screen.png"
        f.write_bytes(b"\x89PNG")
        ctx = build_multimodal_context([f])

        assert len(ctx.inputs) == 1
        assert ctx.primary_modality == ModalityType.IMAGE

    def test_majority_vote(self, tmp_path: Path) -> None:
        (tmp_path / "a.png").write_bytes(b"\x89PNG")
        (tmp_path / "b.jpg").write_bytes(b"\xff\xd8")
        (tmp_path / "c.py").write_text("x = 1")

        ctx = build_multimodal_context(
            [
                tmp_path / "a.png",
                tmp_path / "b.jpg",
                tmp_path / "c.py",
            ]
        )

        assert len(ctx.inputs) == 3
        assert ctx.primary_modality == ModalityType.IMAGE

    def test_skips_missing_files(self, tmp_path: Path) -> None:
        f = tmp_path / "exists.py"
        f.write_text("pass")
        ctx = build_multimodal_context(
            [
                tmp_path / "ghost.png",
                f,
            ]
        )

        assert len(ctx.inputs) == 1
        assert ctx.inputs[0].description == "exists.py"

    def test_all_missing(self, tmp_path: Path) -> None:
        ctx = build_multimodal_context(
            [
                tmp_path / "a.png",
                tmp_path / "b.jpg",
            ]
        )
        assert ctx.inputs == ()
        assert ctx.primary_modality == ModalityType.TEXT

    def test_tie_broken_by_declaration_order(self, tmp_path: Path) -> None:
        """When modalities tie, the first in ModalityType enum order wins."""
        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "b.png").write_bytes(b"\x89PNG")

        ctx = build_multimodal_context(
            [
                tmp_path / "a.py",
                tmp_path / "b.png",
            ]
        )
        # TEXT comes before IMAGE in ModalityType, so TEXT wins the tie.
        assert ctx.primary_modality == ModalityType.TEXT

    def test_inputs_are_tuple(self, tmp_path: Path) -> None:
        f = tmp_path / "f.py"
        f.write_text("pass")
        ctx = build_multimodal_context([f])
        assert isinstance(ctx.inputs, tuple)

    def test_accepts_string_paths(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("data")
        ctx = build_multimodal_context([str(f)])
        assert len(ctx.inputs) == 1


# ---------------------------------------------------------------------------
# is_multimodal_capable
# ---------------------------------------------------------------------------


class TestIsMultimodalCapable:
    @pytest.mark.parametrize("adapter", ["claude", "gemini"])
    def test_capable_adapters(self, adapter: str) -> None:
        assert is_multimodal_capable(adapter) is True

    @pytest.mark.parametrize(
        "adapter",
        ["codex", "aider", "qwen", "ollama", "generic", "goose", "kilo"],
    )
    def test_incapable_adapters(self, adapter: str) -> None:
        assert is_multimodal_capable(adapter) is False

    def test_case_insensitive(self) -> None:
        assert is_multimodal_capable("Claude") is True
        assert is_multimodal_capable("GEMINI") is True
        assert is_multimodal_capable("Codex") is False

    def test_empty_string(self) -> None:
        assert is_multimodal_capable("") is False
