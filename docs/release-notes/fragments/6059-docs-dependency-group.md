## The docs site now builds through a `uv` dependency-group

`uv run --group docs mkdocs build --strict` previously failed before MkDocs
started because `pyproject.toml` declared no `docs` dependency-group. The
group now pins the MkDocs Material toolchain and the `redirects` and `minify`
plugins at the same floors as `docs/requirements.in`, so the site builds with
the project's own lockfile instead of the standalone pin file (#6059).
