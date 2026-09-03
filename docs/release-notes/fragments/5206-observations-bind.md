## Checkpoints bind to the observations they were derived from

A checkpoint captured the grant it was written under but not the actual bytes its suspended work was derived from. If those files moved while the checkpoint sat, the resume would continue on a stale world model that no permission comparison could detect.

`park_task` now content-hashes uncommitted files in the worktree and records the hash on the checkpoint. `evaluate_observations` re-derives those hashes at resume and returns a discard verdict naming what moved; `bernstein resume` stops before the resume_count bump, points at `--discard`, and accepts `--override-observations`. The continuation row records the observations hash and whether the resume was overridden, so later readers can distinguish an overridden resume from a clean one.

(#5206)