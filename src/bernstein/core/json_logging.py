"""JSON structured logging for Bernstein components."""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any


class JsonFormatter(logging.Formatter):
    """Formatter that outputs log records as JSON objects.

    Fields:
        timestamp: ISO8601 formatted time.
        level: Logging level (INFO, ERROR, etc).
        component: Logger name.
        task_id: Associated task ID from CorrelationFilter.
        agent_id: Associated agent session ID from CorrelationFilter.
        correlation_id: Unique workflow ID from CorrelationFilter.
        message: The log message.
        exception: Traceback if an exception occurred.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as JSON.

        Args:
            record: The log record to format.

        Returns:
            JSON-formatted string.
        """
        # Base fields
        log_data: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }

        # Context fields from CorrelationFilter
        for field in ["task_id", "agent_id", "correlation_id"]:
            if hasattr(record, field):
                log_data[field] = getattr(record, field)
            else:
                log_data[field] = "none"

        # Exception info
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Allow passing extra fields via logger.info(..., extra={"key": "val"})
        # We avoid overwriting base fields.
        if hasattr(record, "__dict__"):
            # Standard LogRecord attributes to ignore
            standard_attrs = {
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module", "msecs",
                "message", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName",
                "task_id", "agent_id", "correlation_id", "timestamp", "level", "component"
            }
            for key, val in record.__dict__.items():
                if key not in standard_attrs:
                    log_data[key] = val

        return json.dumps(log_data)


def setup_json_logging(level: int = logging.INFO) -> None:
    """Configure root logger to use JSON formatting.

    This function should be called early in the application lifecycle.
    It respects the BERNSTEIN_LOG_JSON environment variable.

    Args:
        level: Minimum logging level.
    """
    if os.environ.get("BERNSTEIN_LOG_JSON", "").lower() not in ("1", "true", "yes"):
        return

    from bernstein.core.correlation import CorrelationFilter

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Add new JSON handler to stderr
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationFilter())
    root_logger.addHandler(handler)

    logging.info("JSON structured logging enabled")
