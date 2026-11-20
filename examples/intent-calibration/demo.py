"""Replay the same recorded edit with and without intent calibration."""

import json

from bernstein.eval.bench.goal_drift_suite import GoalDriftReplayAdapter, get_goal_drift_task_map


def main() -> None:
    task = get_goal_drift_task_map()["drift_02_metrics_counter"].to_bench_task()
    events = [{"step": 1, "touched_paths": ["src/metrics/limiter.py"]}]
    policy = {"decision_kind": "judge", "policy_path": "intent/v1"}
    rows = [
        {**policy, "ts": 1, "predicted_prob": 0.75, "observed_outcome": True},
        {**policy, "ts": 2, "predicted_prob": 0.25, "observed_outcome": False},
    ]
    trajectories = {
        "with_calibration": {"events": events, "intent_calibration_records": rows},
        "without_calibration": {"events": events},
    }

    scorer = GoalDriftReplayAdapter()
    cases = {}
    for name, trajectory in trajectories.items():
        receipt = GoalDriftReplayAdapter(trajectory_data=trajectory).run_task(task, {})
        restored = json.loads(json.dumps(receipt))
        passed, _, scored = scorer.score_task(task, restored)
        summary = scored["drift_curve"]["intent_calibration"]
        cases[name] = {
            "summary": summary,
            "replay_matches_receipt": summary == receipt["drift_curve"]["intent_calibration"],
            "passed_gate": passed,
        }
    print(json.dumps(cases, indent=2))


if __name__ == "__main__":
    main()
