"""Exclusive process ownership, acquired before crash recovery or writes."""

from __future__ import annotations

import hashlib
from pathlib import Path


class RuntimeLease:
    def __init__(self, database_url: str | None, schema: str, sqlite_path: str) -> None:
        self.connection = None
        self.file = None
        self.closed = False
        if database_url:
            import psycopg

            connection = psycopg.connect(database_url, autocommit=True)
            key = int.from_bytes(
                hashlib.sha256(f"autotrade-owner:{schema}".encode()).digest()[:8],
                "big",
                signed=True,
            )
            result = connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (key,)
            ).fetchone()
            if not result or not result[0]:
                connection.close()
                raise RuntimeError("This journal already has an operator process")
            self.connection = connection
        elif sqlite_path not in {"", ":memory:"}:
            import fcntl

            path = Path(sqlite_path).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.with_suffix(path.suffix + ".owner.lock").open("a")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                handle.close()
                raise RuntimeError(
                    "This journal already has an operator process"
                ) from error
            self.file = handle

    def assert_owned(self) -> None:
        if self.closed:
            raise RuntimeError("journal ownership was released")
        if self.connection is not None:
            # A lost session releases advisory locks. Never reconnect here.
            self.connection.execute("SELECT 1")
        elif self.file is not None and self.file.closed:
            raise RuntimeError("journal ownership was released")

    def close(self) -> None:
        self.closed = True
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.file is not None:
            self.file.close()
            self.file = None
