## Doctor environment snapshots remain statically typed

Doctor's adapter lookup narrows an optional environment mapping where it reads
PATH, clearing an advisory mypy error without an ignore or a cast. Explicit
paths, including an empty PATH, and fallback to the ambient environment retain
their existing behavior.
