### Fixed

- **create_identity** now validates that a child identity's `task_ids` and `allowed_files` are subsets of its parent's scope. When `parent_identity_id` is provided, the operation raises `ValueError` if the child would hold broader permissions than the parent. This prevents a child identity from minting credentials for tasks or files its parent never had access to. ([#5046](https://github.com/sipyourdrink-ltd/bernstein/issues/5046))
