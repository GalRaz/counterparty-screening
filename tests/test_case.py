import json

import pytest

from screening.case import Case
from screening.subjects import Subject
from tests.conftest import NOW


def test_create_and_load(cases_dir):
    c = Case.create(cases_dir, "GBC-BTN-2026-002", "GMC Authority", NOW)
    assert (cases_dir / "GBC-BTN-2026-002" / "meta.json").exists()
    again = Case.load(cases_dir, "GBC-BTN-2026-002")
    assert again.commissioning_party == "GMC Authority"
    with pytest.raises(FileExistsError):
        Case.create(cases_dir, "GBC-BTN-2026-002", "x", NOW)
    with pytest.raises(FileNotFoundError):
        Case.load(cases_dir, "NOPE")


def test_add_subject_writes_files_and_record(cases_dir):
    c = Case.create(cases_dir, "E1", "P", NOW)
    slug = c.add_subject(Subject(type="organization", name="Acme Pte Ltd", jurisdiction="SG"), NOW)
    assert slug == "acme-pte-ltd"
    assert c.slugs() == ["acme-pte-ltd"]
    assert c.subject(slug).jurisdiction == "SG"
    rec = c.record(slug)
    assert rec["engagement"] == "E1" and rec["subject"]["type"] == "organization"
    rec["coverage_gaps"].append("x")
    c.save_record(slug, rec)
    assert json.loads((c.root / slug / "record.json").read_text())["coverage_gaps"] == ["x"]


def test_duplicate_subject_rejected(cases_dir):
    c = Case.create(cases_dir, "E1", "P", NOW)
    c.add_subject(Subject(type="person", name="Jane Doe"), NOW)
    with pytest.raises(FileExistsError):
        c.add_subject(Subject(type="person", name="jane doe"), NOW)
