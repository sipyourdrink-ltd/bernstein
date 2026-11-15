## Update reviewdog/action-actionlint to v1.83.0

The workflow lint job uses action-actionlint v1.83.0, whose Docker action pins its image by digest. It keeps the `github-check` reporter, error failure threshold and disabled embedded shellcheck setting. The existing Ubuntu runner supplies Docker.
