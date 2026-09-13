"""On-disk layout for one engagement: cases/<engagement>/{meta.json, <slug>/subject.json, <slug>/record.json, summary.md}."""
from __future__ import annotations

import json
from pathlib import Path

from screening import record as R
from screening.subjects import Subject


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path):
    return json.loads(path.read_text())


class Case:
    def __init__(self, root: Path, meta: dict):
        self.root = root
        self.meta = meta

    @property
    def engagement(self) -> str:
        return self.meta["engagement"]

    @property
    def commissioning_party(self) -> str:
        return self.meta["commissioning_party"]

    @property
    def summary_path(self) -> Path:
        return self.root / "summary.md"

    @classmethod
    def create(cls, cases_dir: Path, engagement: str, commissioning_party: str, now: str) -> "Case":
        root = cases_dir / engagement
        if root.exists():
            raise FileExistsError(f"case {engagement} already exists at {root}")
        root.mkdir(parents=True)
        meta = {"engagement": engagement, "commissioning_party": commissioning_party,
                "created_at": now, "subjects": []}
        _write_json(root / "meta.json", meta)
        return cls(root, meta)

    @classmethod
    def load(cls, cases_dir: Path, engagement: str) -> "Case":
        root = cases_dir / engagement
        if not (root / "meta.json").exists():
            raise FileNotFoundError(f"no case {engagement} under {cases_dir}")
        return cls(root, _read_json(root / "meta.json"))

    def _save_meta(self) -> None:
        _write_json(self.root / "meta.json", self.meta)

    def add_subject(self, subject: Subject, now: str) -> str:
        slug = subject.slug
        d = self.root / slug
        if d.exists():
            raise FileExistsError(f"subject {slug} already in case {self.engagement}")
        d.mkdir()
        _write_json(d / "subject.json", subject.to_dict())
        _write_json(d / "record.json", R.new_record(subject, self.engagement, self.commissioning_party, now))
        self.meta["subjects"].append(slug)
        self._save_meta()
        return slug

    def slugs(self) -> list[str]:
        return list(self.meta["subjects"])

    def subject(self, slug: str) -> Subject:
        return Subject.from_dict(_read_json(self.root / slug / "subject.json"))

    def record(self, slug: str) -> dict:
        return _read_json(self.root / slug / "record.json")

    def save_record(self, slug: str, record: dict) -> None:
        _write_json(self.root / slug / "record.json", record)

    def save_subject(self, slug: str, subject: Subject) -> None:
        _write_json(self.root / slug / "subject.json", subject.to_dict())
