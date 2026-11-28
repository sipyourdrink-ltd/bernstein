"""Check the offline render-pair reports and their repeatability."""

import json

from tests.unit.examples import run_demo


def test_render_pairs_report_the_change_and_environment_mismatch(tmp_path):
    first = run_demo("render-pairs", tmp_path)
    assert first.returncode == 0, first.stderr
    pairs = json.loads(first.stdout)

    changed = pairs["changed"]
    assert changed["incomparable_reason"] == ""
    [delta] = changed["deltas"]
    assert delta["element_path"] == "main.title"
    assert delta["property"] == "style.color"
    assert (delta["before"], delta["after"]) == ("navy", "teal")
    assert changed["base_unstable_properties"] == changed["head_unstable_properties"] == {"cssSubgrid": "enabled"}

    incomparable = pairs["incomparable"]
    assert incomparable["incomparable_reason"].startswith("environment descriptor hash differs:")
    assert incomparable["base_descriptor_hash"] != incomparable["head_descriptor_hash"]
    assert incomparable["deltas"] == []
    assert incomparable["base_unstable_properties"] == {"cssSubgrid": "enabled"}
    assert incomparable["head_unstable_properties"] == {"cssSubgrid": "disabled"}

    second = run_demo("render-pairs", tmp_path)
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout
