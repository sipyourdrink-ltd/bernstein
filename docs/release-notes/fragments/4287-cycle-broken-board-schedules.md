## A board carrying a dependency cycle drains instead of wedging

A cycle in `depends_on` left every task on it unschedulable for the rest of the run. The task graph already opened the cycle by dropping one edge deterministically, but the dropped edge stayed in the dependent's `depends_on`, so the orchestrator's readiness filter went on treating it as an unmet dependency and the run idled with open tasks no agent would claim. The filter now counts a broken edge as satisfied, so the cycle drains from the first tick while the warning naming the bad declaration is still logged (#4287).
