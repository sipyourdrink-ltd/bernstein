## A child agent identity can no longer be minted with a wider scope than its parent

`create_identity` compares the `task_ids` and `allowed_files` it is given against
the parent named by `parent_identity_id`, and refuses a child that would hold
what the parent does not. The parent id was previously recorded, serialised and
audited but never compared, so a caller naming a parent could mint a child scoped
to tasks and files the parent itself was refused. File coverage is decided by the
same matcher the merge gate reads `allowed_files` with, so the scope that mints a
credential and the scope that admits its diff cannot disagree — `src` admits the
path `src` and nothing under it, `src/**` admits the tree. An empty scope still
means unrestricted, so an unrestricted parent may mint anything while a restricted
parent may not mint an unrestricted child. Refused at declaration, before a token
is signed (#5046).
