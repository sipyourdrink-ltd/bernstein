## Policy limits fetch only from a configured endpoint

`PolicyLimitsClient` and `managed_policy_limits` no longer carry a built-in API URL. Without an operator-supplied `api_url` the client serves the local cache and the fail-open defaults and opens no network connection; the previous default pointed at a host the project does not operate. Pass `api_url=` to keep the hourly fetch.

Related references now sit on domains the project runs: the generated commit footer's `Co-Authored-By` address, the plan and volunteer-manifest schema `$id`s, and the Helm and demo deployment examples, which install the chart from this repository instead of a chart repository that does not exist.
