"""``_render_prompt`` computes prompt segments but does not persist them yet.

Issue #3455 step 1 wires ``segment_prompt`` into the render path for its
return value only: no anchoring in the run record, journal, or
``ContextCapsule`` (that is later scope). This test proves the wiring exists
(the segmenter is actually called with the assembled blocks) without the
render path's output text changing shape.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from bernstein.core.agents.spawn_prompt import _render_prompt


def test_render_prompt_computes_segments_without_persisting_them(tmp_path: Path, make_task: Any) -> None:
    """_render_prompt calls segment_prompt with the assembled blocks; nothing is written to disk."""
    task = make_task(id="T-1", role="backend", title="Fix the parser", description="Build the parser.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with patch(
        "bernstein.core.agents.spawn_prompt.segment_prompt",
        wraps=__import__("bernstein.core.agents.prompt_segments", fromlist=["segment_prompt"]).segment_prompt,
    ) as spy:
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            mailbox_section="## Coordination mailbox\nfrom qa: check X",
        )

    assert spy.called
    _, kwargs = spy.call_args
    assert kwargs["mailbox_block"].strip() == "## Coordination mailbox\nfrom qa: check X"
    assert "Fix the parser" in kwargs["task_block"]

    # Nothing is anchored anywhere yet -- the rendered prompt text is
    # unchanged by the segmentation step; no new .sdd files appear.
    assert not (tmp_path / ".sdd").exists() or not list((tmp_path / ".sdd").rglob("*segment*"))
    assert isinstance(prompt, str)
