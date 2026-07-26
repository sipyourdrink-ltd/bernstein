"""Shared OpenAPI response block for Server-Sent Events routes.

FastAPI cannot infer this. An SSE handler is typed as returning
``StreamingResponse``, which carries no declared media type, so the
default ``application/json`` is published for it. That is wrong twice
over: the body is ``text/event-stream``, and it never ends.

Publishing the real media type matters beyond documentation. A client
generated from the schema will try to parse an unbounded stream as a
single JSON document, and the nightly contract sweep drove these
operations as ordinary request/response pairs and waited on them until
the per-test timeout fired.

Every route that returns a ``StreamingResponse`` with
``media_type="text/event-stream"`` should pass ``responses=SSE_RESPONSES``
so the published contract matches what is sent.
"""

from __future__ import annotations

from typing import Any

SSE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Server-Sent Events stream. The response body does not terminate.",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    }
}
