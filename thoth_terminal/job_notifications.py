from __future__ import annotations

import logging
from typing import Any

from thoth_terminal.db import configured_database_url, using_postgres


logger = logging.getLogger("thoth.jobs.notifications")
CHANNEL = "thoth_jobs"


def _psycopg_url() -> str:
    return configured_database_url().replace("postgresql+psycopg://", "postgresql://", 1)


class JobNotificationListener:
    """Best-effort PostgreSQL wake-up channel; polling remains authoritative."""

    def __init__(self) -> None:
        self._connection: Any | None = None
        self.available = False
        if using_postgres():
            self._connect()

    def _connect(self) -> None:
        self.close()
        try:
            import psycopg

            connection = psycopg.connect(_psycopg_url(), autocommit=True, connect_timeout=3)
            connection.execute(f"LISTEN {CHANNEL}")
            self._connection = connection
            self.available = True
        except Exception as exc:
            self.available = False
            logger.debug("job notification listener unavailable; polling remains active: %s", exc)

    def wait(self, timeout_s: float, stop_event: object | None = None) -> bool:
        timeout = max(0.0, float(timeout_s or 0.0))
        if timeout <= 0:
            return False
        if not self.available or self._connection is None:
            waiter = getattr(stop_event, "wait", None)
            if callable(waiter):
                return bool(waiter(timeout))
            import time

            time.sleep(timeout)
            return False
        try:
            notification = next(self._connection.notifies(timeout=timeout, stop_after=1), None)
            return notification is not None
        except Exception as exc:
            logger.debug("job notification wait failed; reverting to polling: %s", exc)
            self.close()
            return False

    def close(self) -> None:
        connection, self._connection = self._connection, None
        self.available = False
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
