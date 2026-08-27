## Issue comment filtering for volunteer runs

Issue-seeded runs now fetch and filter GitHub issue comments before including
them in the agent prompt. Maintainer/collaborator comments and comments marked
with `bernstein-context:` are always included; remaining budget fills with
newest other comments. This closes the gap between what a reviewer saw in the
issue thread and what the model receives (#4516, #4652).
