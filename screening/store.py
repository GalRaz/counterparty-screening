"""SQLite store: scanIds (kept forever), cached vendor text (90 days), record index (12 months)."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  key TEXT NOT NULL, scan_id TEXT NOT NULL, provider TEXT NOT NULL,
  subject_type TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS scans_key ON scans(key, created_at);
CREATE TABLE IF NOT EXISTS vendor_text (
  scan_id TEXT PRIMARY KEY, body TEXT NOT NULL, cached_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
  engagement TEXT NOT NULL, slug TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (engagement, slug)
);
"""


def _cutoff(now: str, days: int) -> str:
    return (datetime.fromisoformat(now) - timedelta(days=days)).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self.conn.close()

    def record_scan(self, key: str, scan_id: str, provider: str, subject_type: str, now: str) -> None:
        self.conn.execute("INSERT INTO scans VALUES (?,?,?,?,?)", (key, scan_id, provider, subject_type, now))
        self.conn.commit()

    def find_scan(self, key: str, within_days: int, now: str) -> str | None:
        row = self.conn.execute(
            "SELECT scan_id FROM scans WHERE key=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
            (key, _cutoff(now, within_days)),
        ).fetchone()
        return row[0] if row else None

    def cache_vendor_text(self, scan_id: str, body: str, now: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO vendor_text VALUES (?,?,?)", (scan_id, body, now))
        self.conn.commit()

    def vendor_text(self, scan_id: str) -> str | None:
        row = self.conn.execute("SELECT body FROM vendor_text WHERE scan_id=?", (scan_id,)).fetchone()
        return row[0] if row else None

    def index_record(self, engagement: str, slug: str, path: str, now: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)", (engagement, slug, path, now))
        self.conn.commit()

    def purge(self, now: str, vendor_text_days: int = 90, record_days: int = 365) -> dict:
        vt = self.conn.execute("DELETE FROM vendor_text WHERE cached_at<?", (_cutoff(now, vendor_text_days),)).rowcount
        old = self.conn.execute("SELECT engagement, slug, path FROM records WHERE created_at<?",
                                (_cutoff(now, record_days),)).fetchall()
        for engagement, slug, path in old:
            d = Path(path).parent
            if d.is_dir():
                shutil.rmtree(d)
            self.conn.execute("DELETE FROM records WHERE engagement=? AND slug=?", (engagement, slug))
        self.conn.commit()
        return {"vendor_text": vt, "records": len(old)}
