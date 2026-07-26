"""One status code for "this deployment did not configure that subsystem".

Bernstein ships a large optional surface: SSO, plan mode, webhook
receivers, telemetry ingest. A deployment that does not configure one of
them still serves its routes, and those routes have to answer something.

They used to answer ``503 Service Unavailable``. A ``503`` makes two
assertions, and neither is true here:

* **the server failed.** Nothing failed. The server is healthy and
  answering. It was configured without the subsystem, which is a valid
  deployment, not an incident.
* **the condition is transient.** RFC 9110 scopes ``503`` to temporary
  overload or maintenance. This condition holds for the lifetime of the
  deployment until an operator changes the configuration. Waiting does
  not clear it.

The second assertion has a cost that is not theoretical. Every stock HTTP
client treats ``503`` as retryable, and this repo's own retry policy lists
it in ``retryable_status_codes``. GitHub and GitLab redeliver webhooks on
``5xx`` and stop on ``4xx``. So the old answer told every caller to keep
retrying a request that could never start succeeding on its own.

``404`` says what is true: this deployment serves no such resource. It is
permanent, cacheable, terminal for the client, and it does not disclose
which optional subsystems a deployment could have had.

This does not cover a genuinely transient refusal. A draining server, for
example, is unavailable *for now* and correctly keeps its ``503``.
"""

from __future__ import annotations

#: Status for a route whose subsystem this deployment did not configure.
UNCONFIGURED_STATUS = 404
