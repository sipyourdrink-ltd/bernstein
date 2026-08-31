Fixed the volunteer receipt verification workflow, whose check-run step built
its Python source by shell substitution and failed on every pull request. A
repo-hygiene gate now rejects an unquoted Python heredoc in any workflow.
