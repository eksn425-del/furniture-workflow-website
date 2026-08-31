from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, event, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine

from app.models import Base


class Database:
    SCHEMA_VERSION = "workflow-schema.v8-production-launch-retry"

    def __init__(self, database_path: Path) -> None:
        output_root = database_path.parent.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        resolved_path = (output_root / database_path.name).resolve()
        if resolved_path.parent != output_root:
            raise ValueError("database path must remain directly below its system directory")
        self.path = resolved_path
        url = URL.create("sqlite+pysqlite", database=self.path.as_posix())
        self.engine: Engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        event.listen(self.engine, "connect", self._configure_sqlite)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )

    @staticmethod
    def _configure_sqlite(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            columns = {
                column["name"]
                for column in inspect(connection).get_columns("projects")
            }
            if "brand_name" not in columns:
                connection.execute(text("ALTER TABLE projects ADD COLUMN brand_name VARCHAR(200)"))
            self._add_missing_columns(connection, "production_job_events", {
                "runtime_event_id": "VARCHAR(160)",
                "schema_version": "VARCHAR(64) NOT NULL DEFAULT 'control-event.v1'",
                "source_sequence": "INTEGER",
                "stage": "VARCHAR(64)",
                "items_done": "INTEGER",
                "items_total": "INTEGER",
            })
            self._add_missing_columns(connection, "production_runs", {
                "pid": "INTEGER",
                "command_json": "TEXT",
                "events_path": "TEXT",
                "contract_path": "TEXT",
                "checkpoint_path": "TEXT",
                "queue_position": "INTEGER",
                "worker_key": "VARCHAR(64) NOT NULL DEFAULT 'production-v1'",
                "launch_attempts": "INTEGER NOT NULL DEFAULT 0",
                "heartbeat_at": "TIMESTAMP",
                "claimed_at": "TIMESTAMP",
            })
            self._add_missing_columns(connection, "production_artifacts", {
                "size_bytes": "INTEGER NOT NULL DEFAULT 0",
                "item_count": "INTEGER NOT NULL DEFAULT 0",
                "manifest_schema": "VARCHAR(128)",
            })
            self._add_missing_columns(connection, "site_categories", {
                "source_url": "TEXT NOT NULL DEFAULT ''",
                "count_value": "INTEGER",
                "count_kind": "VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN'",
                "confidence": "FLOAT NOT NULL DEFAULT 0",
                "evidence_json": "TEXT",
                "verified_at": "TIMESTAMP",
                "parent_category_id": "VARCHAR(64)",
                "level": "INTEGER NOT NULL DEFAULT 1",
                "scope_kind": "VARCHAR(32) NOT NULL DEFAULT 'CATEGORY'",
            })
            self._add_missing_columns(connection, "site_scan_runs", {
                "job_id": "VARCHAR(64)",
                "pid": "INTEGER",
                "command_json": "TEXT",
                "result_json": "TEXT",
                "browser_session_id": "VARCHAR(64)",
                "heartbeat_at": "TIMESTAMP",
                "resume_count": "INTEGER NOT NULL DEFAULT 0",
            })
            self._add_missing_columns(connection, "production_jobs", {
                "candidate_pool_path": "TEXT",
                "ready_count": "INTEGER NOT NULL DEFAULT 0",
                "provider_qualification_version": "VARCHAR(64)",
            })
            self._add_missing_columns(connection, "provider_safety_checks", {
                "qualification_receipt_json": "TEXT",
                "authorization_hash": "VARCHAR(64)",
            })
            connection.execute(text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version VARCHAR(128) PRIMARY KEY, "
                "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            ))
            connection.execute(
                text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (:version)"),
                {"version": self.SCHEMA_VERSION},
            )

    @staticmethod
    def _add_missing_columns(connection, table_name: str, definitions: dict[str, str]) -> None:
        existing = {column["name"] for column in inspect(connection).get_columns(table_name)}
        for name, definition in definitions.items():
            if name not in existing:
                connection.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{name}" {definition}'))

    def dispose(self) -> None:
        self.engine.dispose()
