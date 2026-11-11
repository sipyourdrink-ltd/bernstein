"""Property: the dependency predicate fires on every coupling signal, in both
admission orders, and never on disjoint tasks.

dependency.py is the issue's "dependency from lineage" deliverable; if a
signal only fires in one order, admission order determines what the check
sees.
"""

from bernstein.core.lineage.dependency import ChangeFact, TaskEffects, coupled


def test_coupled_covers_each_signal_in_both_orders_and_rejects_disjoint():
    # config write-under-reader, reader admitted first (key-level branch only:
    # reader's `reads` deliberately empty so the path-level branch can't fire)
    reader = TaskEffects(
        task_id="r",
        facts=(ChangeFact(kind="config-read", path="src/x.py", key="k"),),
    )
    writer = TaskEffects(
        task_id="w",
        facts=(ChangeFact(kind="config-write", path="cfg.yaml", key="k", value="v"),),
    )
    assert coupled(reader, writer)
    assert coupled(writer, reader)

    # weakened-test target touched, modifier admitted first
    modifier = TaskEffects(task_id="m", writes=frozenset({"src/lim.py"}))
    weaker = TaskEffects(
        task_id="t",
        writes=frozenset({"tests/test_lim.py"}),
        facts=(ChangeFact(kind="weaken-test", path="tests/test_lim.py", test_target="src/lim.py"),),
    )
    assert coupled(modifier, weaker)
    assert coupled(weaker, modifier)

    # symbol-level: definition removed in one task, reference in the other
    defn = TaskEffects(
        task_id="d",
        facts=(ChangeFact(kind="remove-definition", path="a.py", symbol="guard_fn"),),
    )
    ref = TaskEffects(
        task_id="c",
        facts=(ChangeFact(kind="remove-reference", path="b.py", symbol="guard_fn"),),
    )
    assert coupled(defn, ref)
    assert coupled(ref, defn)

    # disjoint tasks: no signal, not coupled
    assert not coupled(
        TaskEffects(task_id="a", writes=frozenset({"p.py"})),
        TaskEffects(task_id="b", writes=frozenset({"q.py"})),
    )
