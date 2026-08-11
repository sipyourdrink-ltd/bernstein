"""The published terminal render has to keep matching what the code draws.

`docs/assets/tui-live.svg` is produced by driving the real `bernstein live`
dashboard against a committed fixture, so it is a claim about the current code
rather than a photograph of some past version. These tests are what keeps the
claim true: they re-render and compare, and they check the two properties the
comparison rests on - that the export is namespaced deterministically, and that
the fixture pins the clock.

The script under test is loaded via importlib (the pattern
`tests/unit/test_context_staleness.py` uses) so the gate and the test exercise
one implementation rather than two that agree today.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "render_tui_snapshot.py"
RENDER_PATH = REPO_ROOT / "docs" / "assets" / "tui-live.svg"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "tui_live_frame.json"


@pytest.fixture(scope="module")
def snapshot() -> Any:
    """Load scripts/render_tui_snapshot.py without executing main()."""
    spec = importlib.util.spec_from_file_location("render_tui_snapshot_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fixture_pins_the_clock() -> None:
    """Without a frozen instant every elapsed-time cell renders differently.

    This is the premise of the whole gate rather than an incidental field: a
    fixture without it produces a render that drifts once a second, which would
    turn the comparison into noise and get the check deleted.
    """
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(fixture.get("frozen_now"), (int, float))


def test_the_committed_render_carries_no_per_export_identifier(snapshot: Any) -> None:
    """Rich namespaces each export with a number that changes every process.

    It says nothing about what was drawn, so it is pinned to a constant. If a
    raw one ever lands in the committed file, byte comparison starts failing
    for a reason that has nothing to do with the dashboard.
    """
    committed = RENDER_PATH.read_text(encoding="utf-8")
    assert snapshot._STABLE_ID in committed
    assert not re.search(r"terminal-\d+", committed)


def test_the_committed_render_matches_what_the_dashboard_draws(snapshot: Any) -> None:
    """The gate itself: re-render the fixture and compare to what is published."""
    current = snapshot.render()
    committed = RENDER_PATH.read_text(encoding="utf-8")
    assert current == committed, (
        "docs/assets/tui-live.svg no longer matches the dashboard.\n"
        f"{snapshot.drift_report(committed, current)}\n"
        "Regenerate with: uv run python scripts/render_tui_snapshot.py --update"
    )


#: (label, text to edit, what to edit it to) per region. The same seeded task
#: title appears in both side-by-side panels - once as a row of the tasks
#: table, once inside the agent's log - which is exactly the case a row-only
#: attribution gets wrong, so the two are distinguished by their surroundings.
REGION_PROBES = (
    ("TASKS", ">&#160;Fix&#160;off-by-one", ">&#160;Fix&#160;off-by-two"),
    ("AGENTS", ">&#160;&#160;&#160;→&#160;Fix&#160;off-by-one", ">&#160;&#160;&#160;→&#160;Fix&#160;off-by-two"),
    ("ACTIVITY", "config&#160;reloaded", "config&#160;refreshed"),
)


@pytest.mark.parametrize(("region", "probe", "replacement"), REGION_PROBES, ids=[name for name, _, _ in REGION_PROBES])
def test_drift_is_attributed_to_the_region_it_happened_in(
    snapshot: Any, region: str, probe: str, replacement: str
) -> None:
    """ "The SVG differs" tells the reader nothing about where to look.

    The dashboard draws AGENTS and TASKS side by side, so a terminal row spans
    both: attribution has to read the column, not just the row, or a change in
    the tasks table gets reported against the agents panel.
    """
    committed = RENDER_PATH.read_text(encoding="utf-8")
    assert probe in committed, f"the {region} probe no longer matches the render"
    mutated = committed.replace(probe, replacement, 1)
    assert mutated != committed

    assert snapshot.regions_changed(mutated, committed) == [region]


def test_the_drift_report_shows_the_lines_and_not_only_the_region(snapshot: Any) -> None:
    """A region name alone still leaves the reader diffing an SVG by hand."""
    committed = RENDER_PATH.read_text(encoding="utf-8")
    mutated = committed.replace(">&#160;Fix&#160;off-by-one", ">&#160;Fix&#160;off-by-two", 1)

    report = snapshot.drift_report(mutated, committed)

    assert report.startswith("changed regions: TASKS")
    assert "off-by-one" in report, "the report should show the differing lines, not just name them"


def test_text_layer_recovers_the_rows_the_dashboard_drew(snapshot: Any) -> None:
    """The region names are only as good as the row reconstruction under them."""
    lines = snapshot.text_layer(RENDER_PATH.read_text(encoding="utf-8"))

    assert any("AGENTS" in line for line in lines)
    assert any("TASKS" in line for line in lines)
    # The fixture is a real demo frame: its seeded task titles have to survive
    # the round trip from SVG text nodes back into rows.
    assert any("get_item route" in line for line in lines)


def test_the_published_render_fetches_nothing_from_a_third_party(snapshot: Any) -> None:
    """A render that phones home on view is not a self-contained artefact.

    Rich's export points its webfont at a CDN. Published in a repository, that
    tells the CDN who is reading the docs and lets a third party change how the
    committed asset looks without the committed bytes changing - and it breaks
    on the air-gapped installs this project ships a profile for.
    """
    committed = RENDER_PATH.read_text(encoding="utf-8")

    # Scoped to things a renderer would fetch. The SVG namespace declaration
    # and Rich's generator comment are both plain text that names something,
    # not a request, and removing them would break the document.
    fetched = re.findall(r"""url\(["']?(https?://[^"')]+)""", committed)
    fetched += re.findall(r"""(?:xlink:)?href=["'](https?://[^"']+)""", committed)

    assert fetched == []
    assert 'local("FiraCode-Regular")' in committed, "the local font source is what still names the face"


def test_every_clip_path_the_render_references_is_defined(snapshot: Any) -> None:
    """A dangling clip reference is resolved by not clipping at all."""
    committed = RENDER_PATH.read_text(encoding="utf-8")
    defined = {int(match.group(1)) for match in snapshot._CLIP_DEF.finditer(committed)}
    referenced = {int(match.group(1)) for match in snapshot._CLIP_REF.finditer(committed)}

    assert referenced - defined == set()


def test_a_missing_row_clip_is_derived_from_the_grid_above_it(snapshot: Any) -> None:
    """The repair extrapolates the row, rather than guessing a rectangle."""
    stable = snapshot._STABLE_ID
    rows = "\n".join(
        f'<clipPath id="{stable}-line-{index}">\n'
        f'    <rect x="0" y="{100.0 + index * 10.0}" width="500" height="9.5"/>\n'
        f"            </clipPath>"
        for index in range(3)
    )
    svg = f'{rows}\n<g clip-path="url(#{stable}-line-3)"></g>'

    repaired = snapshot.complete_line_clips(svg)

    assert f'<clipPath id="{stable}-line-3">' in repaired
    assert '<rect x="0" y="130.0" width="500" height="9.5"/>' in repaired
    # Nothing to repair leaves the bytes untouched, or the gate would rewrite
    # every render it verified.
    assert snapshot.complete_line_clips(repaired) == repaired


def test_dropping_the_remote_font_keeps_the_local_one(snapshot: Any) -> None:
    """The face still has a name to match against an installed font."""
    block = (
        "    @font-face {\n"
        '        font-family: "Fira Code";\n'
        '        src: local("FiraCode-Regular"),\n'
        '                url("https://cdn.example/FiraCode-Regular.woff2") format("woff2"),\n'
        '                url("https://cdn.example/FiraCode-Regular.woff") format("woff");\n'
        "    }\n"
    )

    stripped = snapshot.drop_remote_fonts(block)

    assert "https://" not in stripped
    assert 'src: local("FiraCode-Regular");' in stripped


class _MkDocsLoader(yaml.SafeLoader):
    """Read mkdocs.yml without executing its Python-object tags.

    The config carries `!!python/object/apply:` tags for the slugifier, which
    the safe loader refuses and the unsafe one would import. Neither is needed
    to read the nav, so unknown tags resolve to None.
    """


_MkDocsLoader.add_multi_constructor("", lambda loader, suffix, node: None)


def _nav_targets(node: object) -> list[str]:
    """Flatten every page target in a MkDocs nav tree."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [target for item in node for target in _nav_targets(item)]
    if isinstance(node, dict):
        return [target for value in node.values() for target in _nav_targets(value)]
    return []


def test_the_front_page_raster_is_bound_to_the_committed_svg(snapshot: Any) -> None:
    """The README's PNG is the same render, rasterised - and it can rot alone.

    Pixels move between machines, so the raster cannot be byte-gated like the
    SVG; it is bound to the SHA-256 of the SVG it was rasterised from instead.
    This is the gate itself, run against the repository as committed, plus the
    property it rests on: a stale binding is reported, not ignored.
    """
    assert snapshot.PNG_RENDER.exists(), "the front page links docs/assets/tui-agents.png"
    assert snapshot.verify_png_binding() == []

    binding = json.loads(snapshot.PNG_BINDING.read_text(encoding="utf-8"))
    recorded = binding[snapshot.PNG_RENDER.name]["source_sha256"]
    assert recorded == snapshot.svg_digest()


def test_a_raster_from_an_older_svg_is_reported(snapshot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the gated SVG moves and the raster does not, the check must say so."""
    monkeypatch.setattr(snapshot, "svg_digest", lambda: "0" * 64)

    problems = snapshot.verify_png_binding()

    assert problems, "a raster bound to a different SVG must not pass"
    assert "--rasterize" in problems[0]


def test_deleting_the_raster_is_not_a_way_to_pass(
    snapshot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent asset is a broken image on the front page, not one less check.

    The README and both translated front pages link the PNG by raw URL, so
    treating "no file" as "nothing to verify" would let a deletion through the
    gate and onto the project's landing page.
    """
    monkeypatch.setattr(snapshot, "PNG_RENDER", tmp_path / "tui-agents.png")

    problems = snapshot.verify_png_binding()

    assert problems, "a missing raster must not pass"
    assert "--rasterize" in problems[0]
    assert snapshot.main([]) == 1


def test_the_page_that_documents_this_gate_is_reachable_and_names_the_real_commands(tmp_path: Path) -> None:
    """A gate whose regeneration command is undocumented gets guessed at.

    Three things have to hold together, and each fails silently on its own:
    the page exists, MkDocs actually navigates to it (parsed out of the nav
    tree, not matched as a substring - a path in a comment or an unrelated
    value would satisfy that and leave the page orphaned), and the commands it
    prints do what it says they do. A renamed flag leaves the documentation
    telling the next operator to run something that no longer works, which is
    when they reach for a bypass instead.
    """
    page = REPO_ROOT / "docs" / "contributing" / "render-freshness.md"
    assert page.exists(), "the gate's own documentation is missing"

    config = yaml.load((REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=_MkDocsLoader)
    assert "contributing/render-freshness.md" in _nav_targets(config.get("nav")), (
        "the page is not reachable from the MkDocs nav"
    )

    text = page.read_text(encoding="utf-8")
    for script in ("scripts/render_tui_snapshot.py", "scripts/bind_webui_renders.py"):
        assert f"{script} --update" in text, f"the page does not name {script}'s regeneration command"
        assert (REPO_ROOT / script).exists(), f"the page names {script}, which does not exist"


def test_the_documented_update_flag_regenerates_the_render(snapshot: Any, tmp_path: Path) -> None:
    """The command in the docs has to reach the code that writes the artefact.

    Asserting the string appears in a markdown file proves the sentence, not
    the behaviour: `--update` could be renamed and the page would still read
    correctly while the operator's copy-paste fails.
    """
    written = tmp_path / "tui-live.svg"
    snapshot.RENDER = written
    try:
        assert snapshot.main(["--update"]) == 0
    finally:
        snapshot.RENDER = RENDER_PATH

    assert written.read_text(encoding="utf-8") == RENDER_PATH.read_text(encoding="utf-8")
