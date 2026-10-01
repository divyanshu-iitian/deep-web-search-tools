"""Small local cache and audit store. Replace with managed PostgreSQL for teams."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deepsearch.models import IdentityReport, SearchHit


class Storage:
    def __init__(self, data_dir: str):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "search.sqlite3"
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cache (
                    cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_runs_created ON runs(created_at);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def cached_hits(self, key: str) -> list[SearchHit] | None:
        with self._connect() as db:
            row = db.execute("SELECT payload, expires_at FROM cache WHERE cache_key=?", (key,)).fetchone()
        if not row or datetime.fromisoformat(row[1]) <= datetime.now(timezone.utc):
            return None
        return [SearchHit.model_validate(item) for item in json.loads(row[0])]

    def save_hits(self, key: str, hits: list[SearchHit], hours: int):
        expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                       (key, json.dumps([hit.model_dump(mode="json") for hit in hits]), expiry.isoformat()))

    def cached_value(self, key: str):
        with self._connect() as db:
            row = db.execute("SELECT payload, expires_at FROM cache WHERE cache_key=?", (key,)).fetchone()
        if not row or datetime.fromisoformat(row[1]) <= datetime.now(timezone.utc):
            return None
        return json.loads(row[0])

    def save_value(self, key: str, value, hours: int):
        expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value), expiry.isoformat()))

    def save_report(self, report: IdentityReport):
        with self._connect() as db:
            db.execute("INSERT INTO runs VALUES (?,?,?)",
                       (report.run_id, report.model_dump_json(), report.created_at.isoformat()))

    def get_report(self, run_id: str) -> IdentityReport | None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        return IdentityReport.model_validate_json(row[0]) if row else None

    def recent_reports(self, limit: int = 20) -> list[IdentityReport]:
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [IdentityReport.model_validate_json(row[0]) for row in rows]

    def delete_report(self, run_id: str) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM runs WHERE id=?", (run_id,)).rowcount > 0

    def purge_older_than(self, days: int = 30):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as db:
            db.execute("DELETE FROM runs WHERE created_at < ?", (cutoff,))
            db.execute("DELETE FROM cache WHERE expires_at < ?", (datetime.now(timezone.utc).isoformat(),))
