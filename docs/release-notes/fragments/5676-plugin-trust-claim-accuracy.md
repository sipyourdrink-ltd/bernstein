## The PluginTrust docstring cannot drift back into claiming verification

`PluginTrust.signed` records that a `.signature` file exists, not that any signature validates; `source_verified` records that `pyproject.toml` declares `name`, `version` and `author`, not that provenance was attested. Both the docstring and the rendered panel now say so.

The panel wording is pinned by a test. The class docstring was not, and it is what a reader reaching for `PluginTrust` in an editor actually sees - so the claim has to be false in both places, or the fix only holds where somebody happened to test it (#5676).
