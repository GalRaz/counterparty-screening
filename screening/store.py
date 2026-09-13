"""SQLite store: scanIds (kept forever), cached vendor text (90 days), record index (12 months)."""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
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

    def purge(self, now: str, vendor_text_days: int = 90, record_days: int = 365, *, root: Path) -> dict:
        """Retire expired vendor text and records (§11). Deletes only under `root`; counts the rest as skipped."""
        root = Path(root).resolve()
        vt = self.conn.execute("DELETE FROM vendor_text WHERE cached_at<?", (_cutoff(now, vendor_text_days),)).rowcount
        old = self.conn.execute("SELECT engagement, slug, path FROM records WHERE created_at<?",
                                (_cutoff(now, record_days),)).fetchall()
        records = skipped = engagements_removed = 0
        for engagement, slug, path in old:
            subject_dir = Path(path).resolve().parent
            if root not in subject_dir.parents:
                # A record indexed outside the cases tree is a data problem, not a licence for rmtree.
                # Say which path was kept: a symlinked or relocated subject dir retained in silence
                # looks exactly like a purge that worked.
                skipped += 1
                print(f"purge: kept {subject_dir}; it is not under the cases root {root}", file=sys.stderr)
                continue
            if subject_dir.is_dir():
                try:
                    shutil.rmtree(subject_dir)
                except OSError:
                    skipped += 1
                    continue
            self.conn.execute("DELETE FROM records WHERE engagement=? AND slug=?", (engagement, slug))
            records += 1
            removed, not_retired = _retire_from_engagement(subject_dir.parent, slug, root)
            engagements_removed += removed
            skipped += not_retired
        self.conn.commit()
        return {"vendor_text": vt, "records": records, "engagements_removed": engagements_removed, "skipped": skipped}


def _retire_from_engagement(engagement_dir: Path, slug: str, root: Path) -> tuple[int, int]:
    """Drop the purged slug from meta.json. Returns (engagements_removed, skipped).

    summary.md names the purged subject, so it cannot outlive the record it describes. But an
    engagement directory is only ever removed when two things are true: it sits strictly under the
    cases root (a record indexed one level too high would otherwise make `engagement_dir` the root
    itself), and its meta.json actually lists `subjects`. A meta without that key says nothing about
    who else lives here, and "nothing left" is not the same fact as "we cannot tell".
    """
    if root not in engagement_dir.parents:
        print(f"purge: kept {engagement_dir}; it is not an engagement directory under {root}", file=sys.stderr)
        return 0, 1
    meta_path = engagement_dir / "meta.json"
    if not meta_path.is_file():
        return 0, 0
    try:
        meta = json.loads(meta_path.read_text())
    except ValueError:
        return 0, 0
    if not isinstance(meta.get("subjects"), list):
        return 0, 0
    remaining = [s for s in meta["subjects"] if s != slug]
    if not remaining:
        try:
            shutil.rmtree(engagement_dir)
        except OSError:
            return 0, 0
        print(f"purge: removed engagement {engagement_dir.name}; its last subject was retired")
        return 1, 0
    meta["subjects"] = remaining
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    summary = engagement_dir / "summary.md"
    if summary.is_file():
        summary.unlink()
        print(f"purge: removed {summary}; it named the purged subject — regenerate it with "
              f"`screen report {engagement_dir.name}`")
    return 0, 0
