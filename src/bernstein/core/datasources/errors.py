"""Typed errors for the datasource / query-receipt surface.

Every failure mode an operator can trip has a named exception so callers
(and the CLI) can report *which* rule was violated rather than a generic
``ValueError``. The hierarchy is flat and rooted at :class:`DataSourceError`.
"""

from __future__ import annotations


class DataSourceError(Exception):
    """Base class for every datasource / query-receipt error."""


class UnsupportedStatement(DataSourceError):
    """The submitted SQL is not a single, well-formed statement.

    Raised for multi-statement input, empty input, or anything the read-only
    guard cannot classify as exactly one statement.
    """


class ReadOnlyViolation(DataSourceError):
    """The submitted SQL would mutate data or schema.

    Datasource connections are read-only by contract; any DML (``INSERT`` /
    ``UPDATE`` / ``DELETE`` / ``MERGE`` / ``REPLACE`` ...) or DDL (``CREATE`` /
    ``DROP`` / ``ALTER`` / ``TRUNCATE`` ...) is refused before execution.
    """


class NonCanonicalText(DataSourceError):
    """A text value (or column name) is not NFC-normalised.

    The canonical encoder is fail-closed: it never silently normalises a
    string, because normalisation could collapse two distinct inputs onto the
    same hash. A non-NFC string is rejected so a receipt can only ever attest
    bytes that are already in a single, unambiguous Unicode form.
    """


class UnsupportedValue(DataSourceError):
    """A result cell holds a Python type the canonical encoder cannot render."""


class ConnectionNotFound(DataSourceError):
    """No datasource connection is registered under the requested id."""


class ReceiptNotFound(DataSourceError):
    """No query receipt exists under the requested id."""


class VerificationError(DataSourceError):
    """Offline verification of a receipt failed at a named field."""


__all__ = [
    "ConnectionNotFound",
    "DataSourceError",
    "NonCanonicalText",
    "ReadOnlyViolation",
    "ReceiptNotFound",
    "UnsupportedStatement",
    "UnsupportedValue",
    "VerificationError",
]
