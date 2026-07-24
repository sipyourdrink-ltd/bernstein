"""Operator-configured, read-only datasource connections.

A connection is addressed by a stable ``id``. Its DSN may carry a secret (a
warehouse URI such as ``postgresql://user:pass@host/db``); that secret is never
written into a receipt and never logged. A receipt records only the connection
``id``, and any human-facing rendering of a DSN goes through
:func:`redact_dsn`, which masks the password component.

The baseline driver is ``sqlite`` -- a file path (or ``:memory:``) opened
read-only by :class:`~bernstein.core.datasources.engine.SqliteEngine`. The
config carries a ``driver`` field so a SQLAlchemy-URI / Arrow-interchange
warehouse driver slots in later without changing the receipt format.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.datasources.errors import ConnectionNotFound, DataSourceError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.datasources.engine import QueryEngine

_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")

#: URI userinfo ``scheme://user:password@host`` -- the password is masked.
_USERINFO_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^:/@\s]+):(?P<pw>[^@/\s]+)@")


def redact_dsn(dsn: str) -> str:
    """Return ``dsn`` with any URI password component masked.

    ``postgresql://u:secret@h/db`` -> ``postgresql://u:***@h/db``. A DSN with
    no userinfo password (a plain sqlite path) is returned unchanged. This is
    the only form in which a DSN is ever surfaced to a human or a log line.
    """
    return _USERINFO_RE.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:***@", dsn)


@dataclass(frozen=True, slots=True)
class DataSourceConnection:
    """A read-only datasource connection config.

    Attributes:
        id: Stable connection id (``[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}``).
        driver: Engine driver key. Only ``sqlite`` ships today.
        dsn: Driver-specific data source name. For ``sqlite`` this is a file
            path or ``:memory:``. May carry a secret for warehouse drivers.
        description: Optional human note (never contains secrets by contract).
    """

    id: str
    driver: str
    dsn: str
    description: str = ""

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.id):
            raise DataSourceError(f"invalid connection id: {self.id!r}")
        if self.driver != "sqlite":
            raise DataSourceError(f"unsupported driver {self.driver!r} (only 'sqlite' ships today)")

    @property
    def redacted_dsn(self) -> str:
        """The DSN with any password masked -- safe to log or display."""
        return redact_dsn(self.dsn)

    def to_public_dict(self) -> dict[str, str]:
        """Serialise for display: the DSN is redacted, secrets never leak."""
        return {
            "id": self.id,
            "driver": self.driver,
            "dsn": self.redacted_dsn,
            "description": self.description,
        }

    def to_storage_dict(self) -> dict[str, str]:
        """Serialise for the on-disk registry (full DSN, operator-only file)."""
        return {"id": self.id, "driver": self.driver, "dsn": self.dsn, "description": self.description}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> DataSourceConnection:
        return cls(
            id=str(data["id"]),
            driver=str(data["driver"]),
            dsn=str(data["dsn"]),
            description=str(data.get("description", "")),
        )

    def open_engine(self) -> QueryEngine:
        """Return a read-only engine for this connection."""
        from bernstein.core.datasources.engine import SqliteEngine

        if self.driver == "sqlite":
            return SqliteEngine(self.dsn)
        raise DataSourceError(f"unsupported driver {self.driver!r}")


class ConnectionRegistry:
    """File-backed registry of datasource connections.

    Stored as a single JSON object under ``<root>/connections.json`` at mode
    ``0600`` -- the file can hold DSN secrets, so it is operator-only readable,
    mirroring the lineage log's permission posture.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._path = self.root / "connections.json"

    @property
    def path(self) -> Path:
        return self._path

    def _load_raw(self) -> dict[str, dict[str, str]]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataSourceError(f"cannot read connection registry: {exc}") from exc
        if not isinstance(data, dict):
            raise DataSourceError("connection registry is malformed (expected an object)")
        return data

    def put(self, connection: DataSourceConnection) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        raw = self._load_raw()
        raw[connection.id] = connection.to_storage_dict()
        payload = json.dumps(raw, sort_keys=True, indent=2)
        self._path.write_text(payload, encoding="utf-8")
        # Registry may hold DSN secrets; keep it operator-only readable.
        self._path.chmod(0o600)

    def get(self, connection_id: str) -> DataSourceConnection:
        raw = self._load_raw()
        if connection_id not in raw:
            raise ConnectionNotFound(f"no datasource connection registered under id {connection_id!r}")
        return DataSourceConnection.from_dict(raw[connection_id])

    def list_ids(self) -> list[str]:
        return sorted(self._load_raw())

    def list_connections(self) -> list[DataSourceConnection]:
        raw = self._load_raw()
        return [DataSourceConnection.from_dict(raw[cid]) for cid in sorted(raw)]


__all__ = [
    "ConnectionRegistry",
    "DataSourceConnection",
    "redact_dsn",
]
