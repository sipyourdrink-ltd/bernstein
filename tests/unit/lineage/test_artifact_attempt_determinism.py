"""Hash-seed determinism of the artifact-attempt chain (issue #3071).

``_concrete_keys`` accumulates declared outputs into a ``set`` and returns
``tuple(sorted(...))``. The sort is load-bearing rather than cosmetic:
:func:`reconcile_declared_outputs` walks that tuple in order and appends one
spine entry per key, and each entry hashes its predecessor. Drop the sort and
the chain the run produces becomes a function of ``PYTHONHASHSEED``, so two
replays of the same declaration on the same inputs disagree on the head hash.

Nothing in the existing suite could see that. ``tests/unit/lineage/
test_artifact_attempt.py`` and ``tests/property/test_artifact_health_properties.py``
are the only files that touch this module, and both compare orders *inside one
interpreter at one hash seed* - the exact axis a randomised ``set`` iteration is
invariant along. Removing the sort left all 37 of those tests green.

So the probe runs in a child interpreter under an explicit ``PYTHONHASHSEED``
and compares the artefact the guarantee is about: the spine head hash, plus the
key order the reconciliation reports. Both are pinned because they can break
independently - a later edit that sorts the *returned* list while appending in
set order would keep the reported order stable and still fork the chain.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Seeds the issue measured the divergence at, plus one more. ``0`` disables
# randomisation entirely, so it is the odd one out and worth keeping.
_HASH_SEEDS = ("0", "1", "12345", "777")

# Eight declared outputs whose canonical keys are distinct strings, which is
# what makes ``set`` iteration order seed-dependent. Any smaller set risks a
# collision-free layout that happens to iterate in sorted order under every
# seed we try, which would make the probe pass for the wrong reason.
_DECLARED = [
    "src/pkg/mod_alpha.py",
    "src/pkg/mod_beta.py",
    "src/pkg/mod_gamma.py",
    "src/pkg/mod_delta.py",
    "src/pkg/mod_epsilon.py",
    "src/pkg/mod_zeta.py",
    "src/pkg/mod_eta.py",
    "src/pkg/mod_theta.py",
]

_PROBE = """
import json, sys, tempfile
sys.path.insert(0, {src!r})
from pathlib import Path

from bernstein.core.lineage.artifact_attempt import reconcile_declared_outputs

declared = {declared!r}
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "lineage"
    recorded = reconcile_declared_outputs(
        root,
        run_id="run-determinism",
        declared=declared,
        task_id="task-1",
        actor="tester",
        model="test-model",
        hmac_key=b"determinism-probe-key-0123456789",
        timestamp=0,
        outcome="failed",
        reason="probe",
    )
    head = json.loads((root / "run-determinism" / "spine.head").read_text(encoding="utf-8"))

print(json.dumps({{"recorded": list(recorded), "head": head}}, sort_keys=True))
"""


def _probe_under_seed(seed: str) -> str:
    """Run the reconciliation in a child interpreter at ``PYTHONHASHSEED=seed``."""
    src = str(Path(__file__).resolve().parents[3] / "src")
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = seed
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(src=src, declared=_DECLARED)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return proc.stdout.strip()


def test_attempt_chain_head_is_identical_across_hash_seeds() -> None:
    """The spine head over the same declaration does not depend on the hash seed.

    Fails with four distinct head hashes when ``sorted()`` is removed from
    ``_concrete_keys``, which is the guarantee this pins.
    """
    outputs = {seed: _probe_under_seed(seed) for seed in _HASH_SEEDS}
    heads = {seed: json.loads(out)["head"]["head_hash"] for seed, out in outputs.items()}
    assert len(set(heads.values())) == 1, f"attempt chain head diverged across hash seeds: {heads}"


def test_recorded_key_order_is_identical_across_hash_seeds() -> None:
    """The reported key order is the sorted order at every hash seed."""
    orders = {seed: json.loads(_probe_under_seed(seed))["recorded"] for seed in _HASH_SEEDS}
    distinct = {tuple(order) for order in orders.values()}
    assert len(distinct) == 1, f"recorded key order diverged across hash seeds: {orders}"
    assert next(iter(distinct)) == tuple(sorted(_DECLARED))


def test_declaration_order_does_not_change_the_chain() -> None:
    """Two spellings of one declaration set produce the same head.

    Complements the seed axis: the sort has to normalise the *caller's* order
    too, not only the interpreter's iteration order.
    """
    src = str(Path(__file__).resolve().parents[3] / "src")
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    heads: list[str] = []
    for declared in (_DECLARED, list(reversed(_DECLARED))):
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE.format(src=src, declared=declared)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        heads.append(json.loads(proc.stdout)["head"]["head_hash"])
    assert heads[0] == heads[1], f"declaration order changed the chain head: {heads}"
