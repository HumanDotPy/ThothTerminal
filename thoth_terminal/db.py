from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import shutil
import sqlite3
from threading import RLock
import time
from typing import Generator, Iterable
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from thoth_terminal.config import ensure_data_dirs, settings

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker | None = None
_ENGINE_INIT_LOCK = RLock()
_WRITE_LOCK = RLock()
_WRITE_LOCK_TIMEOUT_S = 10
_LEGACY_DB_FILENAME = "thoth.db"
_DB_SIDE_SUFFIXES = ("-shm", "-wal")
_DB_FILE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_DEFAULT_COMPACT_MIN_RECLAIMABLE_BYTES = 128 * 1024 * 1024


@dataclass
class DatabaseQueryMetrics:
    """Low-overhead query totals for one explicitly instrumented operation."""

    query_count: int = 0
    sql_time_s: float = 0.0


_ACTIVE_QUERY_METRICS: ContextVar[DatabaseQueryMetrics | None] = ContextVar(
    "thoth_active_database_query_metrics",
    default=None,
)


@contextmanager
def track_database_queries() -> Iterable[DatabaseQueryMetrics]:
    """Collect SQL count/time in the current context until the block exits."""

    metrics = DatabaseQueryMetrics()
    token = _ACTIVE_QUERY_METRICS.set(metrics)
    try:
        yield metrics
    finally:
        _ACTIVE_QUERY_METRICS.reset(token)


def _record_query_start(execution_context) -> None:
    metrics = _ACTIVE_QUERY_METRICS.get()
    if metrics is None:
        return
    metrics.query_count += 1
    execution_context._thoth_query_metrics_started_at = time.perf_counter()


def _record_query_end(execution_context) -> None:
    metrics = _ACTIVE_QUERY_METRICS.get()
    started_at = getattr(execution_context, "_thoth_query_metrics_started_at", None)
    if metrics is None or started_at is None:
        return
    metrics.sql_time_s += max(0.0, time.perf_counter() - started_at)
    execution_context._thoth_query_metrics_started_at = None


class DatabaseBusyError(RuntimeError):
    pass

_SQLITE_BUSY_TIMEOUT_S = 10


def configured_database_url() -> str:
    db_url = str(getattr(settings, "database_url", "") or "").strip()
    if db_url:
        return db_url
    db_path = Path(settings.db_path).resolve(strict=False)
    return f"sqlite:///{db_path.as_posix()}"


def database_backend(url: str | None = None) -> str:
    candidate = str(url or configured_database_url()).strip().lower()
    if candidate.startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
        return "postgresql"
    if candidate.startswith("sqlite"):
        return "sqlite"
    return candidate.split(":", 1)[0] or "unknown"


def using_sqlite() -> bool:
    return database_backend() == "sqlite"


def using_postgres() -> bool:
    return database_backend() == "postgresql"


def _postgres_session_options() -> tuple[int, int]:
    idle_timeout_ms = max(0, int(getattr(settings, "postgres_idle_in_transaction_timeout_ms", 120_000) or 0))
    lock_timeout_ms = max(0, int(getattr(settings, "postgres_lock_timeout_ms", 30_000) or 0))
    return idle_timeout_ms, lock_timeout_ms


def _postgres_startup_options() -> str:
    idle_timeout_ms, lock_timeout_ms = _postgres_session_options()
    options: list[str] = []
    if idle_timeout_ms > 0:
        options.append(f"-c idle_in_transaction_session_timeout={idle_timeout_ms}")
    if lock_timeout_ms > 0:
        options.append(f"-c lock_timeout={lock_timeout_ms}")
    return " ".join(options)


def _apply_postgres_session_options(dbapi_connection) -> None:
    idle_timeout_ms, lock_timeout_ms = _postgres_session_options()
    if idle_timeout_ms <= 0 and lock_timeout_ms <= 0:
        return
    cursor = dbapi_connection.cursor()
    try:
        if idle_timeout_ms > 0:
            cursor.execute("SELECT set_config('idle_in_transaction_session_timeout', %s, false)", (str(idle_timeout_ms),))
        if lock_timeout_ms > 0:
            cursor.execute("SELECT set_config('lock_timeout', %s, false)", (str(lock_timeout_ms),))
    finally:
        cursor.close()


def _session_scope_requires_process_write_lock(url: str | None = None) -> bool:
    # SQLite benefits from a process-local writer gate; Postgres should rely on database-native concurrency.
    return database_backend(url) == "sqlite"


def utcnow() -> datetime:
    # Store naive UTC timestamps in SQLite (matches existing DB rows).
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _busy_hint_message(detail: str) -> str:
    base = "Database is busy. Please retry in a moment."
    suggestion = (
        " If running in WSL, direct writes may be blocked by `windows_single_writer`; "
        "run writer services on Windows or use MCP/web for mutations."
    )
    return f"{base} ({detail}).{suggestion}"


def _is_locked_error(exc: OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def _is_disk_io_error(exc: OperationalError) -> bool:
    return "disk i/o error" in str(exc).lower()


def _sqlite_integrity_status(path: Path) -> tuple[bool | None, str]:
    if not path.exists():
        return None, "missing"
    try:
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute("PRAGMA integrity_check;").fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    result = str(row[0]).strip() if row and row[0] is not None else ""
    if result == "ok":
        return True, "ok"
    return False, result or "unknown integrity result"


def _is_db_artifact_file(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(_DB_SIDE_SUFFIXES):
        return False
    if name.endswith(_DB_FILE_SUFFIXES):
        return True
    return any(token in name for token in (".db.", ".sqlite.", ".sqlite3."))


def _existing_db_sidecars(path: Path) -> list[Path]:
    existing: list[Path] = []
    for suffix in _DB_SIDE_SUFFIXES:
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            existing.append(sidecar.resolve(strict=False))
    return existing


def _db_file_report(path: Path, *, role: str, check_integrity: bool = True) -> dict[str, object]:
    resolved = path.resolve(strict=False)
    size_bytes = None
    if resolved.exists():
        try:
            size_bytes = int(resolved.stat().st_size)
        except Exception:
            size_bytes = None
    if check_integrity:
        integrity_ok, integrity_detail = _sqlite_integrity_status(resolved)
    else:
        integrity_ok, integrity_detail = None, "skipped; inactive local database file"
    return {
        "path": str(resolved),
        "exists": bool(resolved.exists()),
        "size_bytes": size_bytes,
        "integrity_ok": integrity_ok,
        "integrity_detail": integrity_detail,
        "role": role,
        "sidecars": [str(sidecar) for sidecar in _existing_db_sidecars(resolved)],
    }


def _scan_roots_for_db_artifacts(*scan_roots: Path) -> list[Path]:
    discovered: list[Path] = []
    seen: set[Path] = set()
    for root in scan_roots:
        root_path = Path(root).resolve(strict=False)
        if not root_path.exists() or not root_path.is_dir():
            continue
        try:
            children = list(root_path.iterdir())
        except Exception:
            continue
        for child in children:
            if not child.is_file():
                continue
            if not _is_db_artifact_file(child):
                continue
            resolved = child.resolve(strict=False)
            if resolved in seen:
                continue
            seen.add(resolved)
            discovered.append(resolved)
    return sorted(discovered, key=lambda item: str(item).lower())


def _archive_destination(archive_root: Path, source: Path, stamp: str) -> Path:
    attempt = 0
    while True:
        suffix = stamp if attempt == 0 else f"{stamp}.{attempt}"
        dest = archive_root / f"{source.name}.{suffix}"
        if not dest.exists():
            return dest
        attempt += 1


def _move_db_artifact_group(
    primary: Path,
    *,
    archive_root: Path,
    stamp: str,
    actions: list[dict[str, object]],
    archived: set[Path],
    kind: str,
) -> None:
    group = [primary.resolve(strict=False), *_existing_db_sidecars(primary.resolve(strict=False))]
    for path in group:
        resolved = path.resolve(strict=False)
        if resolved in archived or (not resolved.exists()):
            continue
        dest = _archive_destination(archive_root, resolved, stamp)
        shutil.move(str(resolved), str(dest))
        archived.add(resolved)
        actions.append(
            {
                "from": str(resolved),
                "to": str(dest),
                "kind": kind,
            }
        )


def create_managed_backup(
    *,
    configured_db_path: Path | None = None,
    backup_dir: Path | None = None,
    backup_filename: str | None = None,
) -> dict[str, object]:
    configured = Path(configured_db_path or settings.db_path).resolve(strict=False)
    backup_root = Path(backup_dir or (configured.parent / "backups")).resolve(strict=False)
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_name = str(backup_filename or f"{configured.stem}.backup.sqlite").strip()
    if not backup_name:
        backup_name = f"{configured.stem}.backup.sqlite"
    backup_path = (backup_root / backup_name).resolve(strict=False)

    if not configured.exists():
        return {
            "configured_path": str(configured),
            "backup_path": str(backup_path),
            "created": False,
            "skipped": "configured database file does not exist",
        }

    tmp_path = backup_path.with_name(f".{backup_path.name}.tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    source_conn: sqlite3.Connection | None = None
    backup_conn: sqlite3.Connection | None = None
    try:
        source_conn = sqlite3.connect(str(configured), timeout=_SQLITE_BUSY_TIMEOUT_S)
        source_conn.execute(f"PRAGMA busy_timeout={int(_SQLITE_BUSY_TIMEOUT_S * 1000)};")
        backup_conn = sqlite3.connect(str(tmp_path), timeout=_SQLITE_BUSY_TIMEOUT_S)
        source_conn.backup(backup_conn)
        backup_conn.commit()
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    finally:
        if backup_conn is not None:
            backup_conn.close()
        if source_conn is not None:
            source_conn.close()

    tmp_path.replace(backup_path)
    return {
        "configured_path": str(configured),
        "backup_path": str(backup_path),
        "created": True,
        "backup": _db_file_report(backup_path, role="backup"),
    }


def database_space_report(*, configured_db_path: Path | None = None) -> dict[str, object]:
    configured = Path(configured_db_path or settings.db_path).resolve(strict=False)
    wal_path = Path(str(configured) + "-wal")
    shm_path = Path(str(configured) + "-shm")
    if not configured.exists():
        return {
            "configured_path": str(configured),
            "exists": False,
            "skipped": "configured database file does not exist",
        }

    conn = sqlite3.connect(str(configured), timeout=_SQLITE_BUSY_TIMEOUT_S)
    try:
        conn.execute(f"PRAGMA busy_timeout={int(_SQLITE_BUSY_TIMEOUT_S * 1000)};")
        page_size = int(conn.execute("PRAGMA page_size;").fetchone()[0] or 0)
        page_count = int(conn.execute("PRAGMA page_count;").fetchone()[0] or 0)
        freelist_count = int(conn.execute("PRAGMA freelist_count;").fetchone()[0] or 0)
        journal_mode = str(conn.execute("PRAGMA journal_mode;").fetchone()[0] or "")
        auto_vacuum = int(conn.execute("PRAGMA auto_vacuum;").fetchone()[0] or 0)
    finally:
        conn.close()

    db_size = int(configured.stat().st_size)
    wal_size = int(wal_path.stat().st_size) if wal_path.exists() else 0
    shm_size = int(shm_path.stat().st_size) if shm_path.exists() else 0
    reclaimable_bytes = int(page_size * freelist_count)
    total_bytes = int(db_size + wal_size + shm_size)
    return {
        "configured_path": str(configured),
        "exists": True,
        "db_size_bytes": db_size,
        "wal_size_bytes": wal_size,
        "shm_size_bytes": shm_size,
        "total_size_bytes": total_bytes,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "reclaimable_bytes": reclaimable_bytes,
        "reclaimable_ratio": (float(reclaimable_bytes) / float(db_size)) if db_size else 0.0,
        "journal_mode": journal_mode,
        "auto_vacuum": auto_vacuum,
    }


def _compact_database_unlocked(
    *,
    configured_db_path: Path | None = None,
    min_reclaimable_bytes: int = _DEFAULT_COMPACT_MIN_RECLAIMABLE_BYTES,
    force_vacuum: bool = False,
    backup_first: bool = True,
) -> dict[str, object]:
    configured = Path(configured_db_path or settings.db_path).resolve(strict=False)
    before = database_space_report(configured_db_path=configured)
    if not before.get("exists"):
        return {
            "configured_path": str(configured),
            "before": before,
            "after": before,
            "actions": [],
            "skipped": before.get("skipped") or "configured database file does not exist",
        }

    actions: list[str] = []
    backup_result: dict[str, object] | None = None

    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None:
        _ENGINE.dispose()
        _ENGINE = None
        _SESSION_FACTORY = None

    if backup_first and (force_vacuum or int(before.get("reclaimable_bytes") or 0) >= int(min_reclaimable_bytes)):
        backup_result = create_managed_backup(configured_db_path=configured)
        if backup_result.get("created"):
            actions.append("backup")

    conn = sqlite3.connect(str(configured), timeout=_SQLITE_BUSY_TIMEOUT_S)
    try:
        conn.execute(f"PRAGMA busy_timeout={int(_SQLITE_BUSY_TIMEOUT_S * 1000)};")
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchall()
        actions.append("wal_checkpoint_truncate")
        conn.execute("PRAGMA optimize;")
        actions.append("optimize")

        reclaimable = int(before.get("reclaimable_bytes") or 0)
        if force_vacuum or reclaimable >= int(min_reclaimable_bytes):
            conn.execute("VACUUM;")
            actions.append("vacuum")
        else:
            checkpoint_text = "; ".join(",".join(str(value) for value in row) for row in checkpoint)
            return {
                "configured_path": str(configured),
                "before": before,
                "after": database_space_report(configured_db_path=configured),
                "actions": actions,
                "backup": backup_result,
                "skipped": (
                    "vacuum not run; reclaimable bytes below threshold "
                    f"({reclaimable} < {int(min_reclaimable_bytes)})"
                ),
                "checkpoint": checkpoint_text,
            }
    finally:
        conn.close()

    return {
        "configured_path": str(configured),
        "before": before,
        "after": database_space_report(configured_db_path=configured),
        "actions": actions,
        "backup": backup_result,
    }


def compact_database(
    *,
    configured_db_path: Path | None = None,
    min_reclaimable_bytes: int = _DEFAULT_COMPACT_MIN_RECLAIMABLE_BYTES,
    force_vacuum: bool = False,
    backup_first: bool = True,
) -> dict[str, object]:
    """Compact SQLite under the same lifecycle and writer gates as sessions.

    Engine teardown must share the initialization lock with ``get_engine`` and
    the process writer lock with write/maintenance sessions.  Otherwise a
    concurrent first-use acquisition can retain a disposed engine or a live
    writer can race WAL checkpoint/VACUUM.
    """

    acquired = _WRITE_LOCK.acquire(timeout=_WRITE_LOCK_TIMEOUT_S)
    if not acquired:
        raise DatabaseBusyError("Database is busy. Please retry in a moment.")
    try:
        with _ENGINE_INIT_LOCK:
            return _compact_database_unlocked(
                configured_db_path=configured_db_path,
                min_reclaimable_bytes=min_reclaimable_bytes,
                force_vacuum=force_vacuum,
                backup_first=backup_first,
            )
    finally:
        _WRITE_LOCK.release()


def audit_database_files(
    *,
    configured_db_path: Path | None = None,
    data_dir: Path | None = None,
    workspace_root: Path | None = None,
    check_integrity: bool | None = None,
) -> dict[str, object]:
    active_url = configured_database_url()
    active_backend = database_backend(active_url)
    check_local_integrity = (
        active_backend == "sqlite" or configured_db_path is not None
        if check_integrity is None
        else bool(check_integrity)
    )
    configured = Path(configured_db_path or settings.db_path).resolve(strict=False)
    data_root = Path(data_dir or settings.data_dir).resolve(strict=False)
    workspace = Path(workspace_root or configured.parent.parent).resolve(strict=False)
    configured_report = _db_file_report(configured, role="configured", check_integrity=check_local_integrity)

    legacy_reports: list[dict[str, object]] = []
    legacy_primary = (data_root / _LEGACY_DB_FILENAME).resolve(strict=False)
    if legacy_primary != configured:
        legacy_reports.append(_db_file_report(legacy_primary, role="legacy", check_integrity=check_local_integrity))

    excluded = {configured, legacy_primary}
    unexpected_reports: list[dict[str, object]] = []
    for candidate in _scan_roots_for_db_artifacts(data_root, workspace):
        if candidate in excluded:
            continue
        unexpected_reports.append(_db_file_report(candidate, role="unexpected", check_integrity=check_local_integrity))

    return {
        "active": {
            "backend": active_backend,
            "database_url_configured": bool(str(getattr(settings, "database_url", "") or "").strip()),
            "url": active_url,
        },
        "configured": configured_report,
        "legacy": legacy_reports,
        "unexpected": unexpected_reports,
    }


def cleanup_legacy_db_files(
    *,
    configured_db_path: Path | None = None,
    data_dir: Path | None = None,
    backup_dir: Path | None = None,
    workspace_root: Path | None = None,
) -> dict[str, object]:
    configured = Path(configured_db_path or settings.db_path).resolve(strict=False)
    data_root = Path(data_dir or settings.data_dir).resolve(strict=False)
    legacy_primary = (data_root / _LEGACY_DB_FILENAME).resolve(strict=False)
    actions: list[dict[str, object]] = []
    archive_root = Path(backup_dir or (data_root / "backups" / "db_artifacts")).resolve(strict=False)
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    archived: set[Path] = set()
    check_local_integrity = database_backend(configured_database_url()) == "sqlite"

    if legacy_primary != configured:
        _move_db_artifact_group(
            legacy_primary,
            archive_root=archive_root,
            stamp=stamp,
            actions=actions,
            archived=archived,
            kind="legacy",
        )

    initial_report = audit_database_files(
        configured_db_path=configured,
        data_dir=data_root,
        workspace_root=workspace_root,
        check_integrity=check_local_integrity,
    )
    for entry in initial_report.get("unexpected", []):
        if not isinstance(entry, dict):
            continue
        path_text = str(entry.get("path") or "").strip()
        if not path_text:
            continue
        _move_db_artifact_group(
            Path(path_text),
            archive_root=archive_root,
            stamp=stamp,
            actions=actions,
            archived=archived,
            kind="unexpected",
        )

    if not actions:
        return {
            "configured_path": str(configured),
            "archive_dir": None,
            "actions": actions,
            "skipped": "no unmanaged database artifacts found",
        }

    report = audit_database_files(
        configured_db_path=configured,
        data_dir=data_root,
        workspace_root=workspace_root,
        check_integrity=check_local_integrity,
    )
    return {
        "configured_path": str(configured),
        "archive_dir": str(archive_root),
        "actions": actions,
        "audit": report,
    }


def get_engine() -> Engine:
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is None or _SESSION_FACTORY is None:
        with _ENGINE_INIT_LOCK:
            if _ENGINE is not None and _SESSION_FACTORY is not None:
                return _ENGINE
            ensure_data_dirs()
            db_url = configured_database_url()
            connect_args = {}
            engine_kwargs = {"pool_pre_ping": True}
            if db_url.startswith("sqlite"):
                connect_args = {"check_same_thread": False, "timeout": _SQLITE_BUSY_TIMEOUT_S}
                engine_kwargs["poolclass"] = NullPool
            elif database_backend(db_url) == "postgresql":
                timeout_s = max(1, int(getattr(settings, "postgres_connect_timeout_s", 5) or 5))
                connect_args = {"connect_timeout": timeout_s}
                startup_options = _postgres_startup_options()
                if startup_options:
                    connect_args["options"] = startup_options
                engine_kwargs |= {
                    "pool_size": 10,
                    "max_overflow": 20,
                    "pool_timeout": 10,
                    "pool_recycle": 1800,
                }
            _ENGINE = create_engine(
                db_url,
                connect_args=connect_args,
                **engine_kwargs,
            )

            @event.listens_for(_ENGINE, "before_cursor_execute")
            def _track_query_start(_conn, _cursor, _statement, _parameters, context, _executemany) -> None:
                _record_query_start(context)

            @event.listens_for(_ENGINE, "after_cursor_execute")
            def _track_query_end(_conn, _cursor, _statement, _parameters, context, _executemany) -> None:
                _record_query_end(context)

            @event.listens_for(_ENGINE, "handle_error")
            def _track_failed_query(exception_context) -> None:
                context = getattr(exception_context, "execution_context", None)
                if context is not None:
                    _record_query_end(context)

            if db_url.startswith("sqlite"):
                @event.listens_for(_ENGINE, "connect")
                def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-redef]
                    cursor = dbapi_connection.cursor()
                    try:
                        cursor.execute("PRAGMA foreign_keys=ON;")
                        cursor.execute(f"PRAGMA busy_timeout={int(_SQLITE_BUSY_TIMEOUT_S * 1000)};")
                        # WAL improves read/write concurrency and reduces "database is locked" under normal load.
                        # If another process holds an incompatible lock, this may fail; ignore and proceed.
                        try:
                            cursor.execute("PRAGMA journal_mode=WAL;")
                        except Exception:
                            pass
                        # Favor durability over speed to reduce risk of corruption on Windows.
                        for pragma in (
                            "PRAGMA synchronous=FULL;",
                            "PRAGMA temp_store=MEMORY;",
                            "PRAGMA locking_mode=NORMAL;",
                        ):
                            try:
                                cursor.execute(pragma)
                            except Exception:
                                pass
                    finally:
                        cursor.close()
            elif database_backend(db_url) == "postgresql":
                @event.listens_for(_ENGINE, "connect")
                def _set_postgres_session_options(dbapi_connection, _connection_record) -> None:  # type: ignore[no-redef]
                    _apply_postgres_session_options(dbapi_connection)

                @event.listens_for(_ENGINE, "checkout")
                def _reset_postgres_session_options(dbapi_connection, _connection_record, _connection_proxy) -> None:  # type: ignore[no-redef]
                    _apply_postgres_session_options(dbapi_connection)

            _SESSION_FACTORY = sessionmaker(bind=_ENGINE, expire_on_commit=False, autoflush=False)
    return _ENGINE


def session_factory() -> sessionmaker:
    get_engine()
    assert _SESSION_FACTORY is not None
    return _SESSION_FACTORY


def get_session() -> Generator[Session, None, None]:
    SessionLocal = session_factory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterable[Session]:
    SessionLocal = session_factory()
    acquired = False
    if _session_scope_requires_process_write_lock():
        acquired = _WRITE_LOCK.acquire(timeout=_WRITE_LOCK_TIMEOUT_S)
        if not acquired:
            raise DatabaseBusyError("Database is busy. Please retry in a moment.")
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except OperationalError as exc:
        session.rollback()
        if _is_locked_error(exc):
            raise DatabaseBusyError(_busy_hint_message("sqlite lock")) from exc
        if _is_disk_io_error(exc):
            raise DatabaseBusyError(
                _busy_hint_message(
                    "sqlite disk I/O error (check Windows/WSL path and single-writer runtime policy)"
                )
            ) from exc
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        if acquired:
            _WRITE_LOCK.release()
