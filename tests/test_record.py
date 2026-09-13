from screening import record as R
from screening.subjects import Identifier, Subject
from tests.conftest import NOW


def make_subject():
    return Subject(type="person", name="Mark Phillips", jurisdiction="AU",
                   identifiers=[Identifier("dob", "1970", "passport copy")])


def test_new_record_shape():
    rec = R.new_record(make_subject(), "GBC-BTN-2026-002", "GMC Authority", NOW)
    assert rec["engagement"] == "GBC-BTN-2026-002"
    assert rec["commissioning_party"] == "GMC Authority"
    assert rec["subject"]["slug"] == "mark-phillips"
    assert rec["subject"]["identifiers_supplied"] == ["dob=1970"]
    assert rec["subject"]["identifier_sources"] == ["dob: passport copy"]
    assert rec["human_review_required"] is True
    assert rec["layer_c"] is None and rec["layer_d"] is None
    assert R.validate(rec) == []


def test_add_coverage_gap_is_idempotent():
    rec = R.new_record(make_subject(), "E", "P", NOW)
    R.add_coverage_gap(rec, "no DOB supplied")
    R.add_coverage_gap(rec, "no DOB supplied")
    assert rec["coverage_gaps"] == ["no DOB supplied"]


def test_media_item_defaults():
    item = R.new_media_item(title="t", publisher="p", published="2025-01-01", url="https://x",
                            retrieved=NOW, retrieval_status="full", identity="possible_subject",
                            corroborator=None, legal_status="allegation", source_type="wire")
    assert item["relevance"] == "unassessed"
    assert item["proposed_disposition"] is None


def test_validate_rejects_reviewed_flags():
    rec = R.new_record(make_subject(), "E", "P", NOW)
    rec["human_review_required"] = False
    rec["watchlist_candidates"].append({"source": "opensanctions", "id": "Q1", "caption": "c",
                                        "score": 0.9, "topics": [], "datasets": [], "assessment": "cleared"})
    errs = R.validate(rec)
    assert any("human_review_required" in e for e in errs)
    assert any("assessment" in e for e in errs)


def test_validate_media_enums_and_corroborator():
    rec = R.new_record(make_subject(), "E", "P", NOW)
    rec["media_items"].append(R.new_media_item(
        title="t", publisher="p", published="2025", url="u", retrieved=NOW,
        retrieval_status="full", identity="confirmed_subject", corroborator=None,
        legal_status="convicted", source_type="primary"))
    rec["media_items"].append(R.new_media_item(
        title="t", publisher="p", published="2025", url="u", retrieved=NOW,
        retrieval_status="pdf", identity="possible_subject", corroborator=None,
        legal_status="rumour", source_type="primary"))
    errs = R.validate(rec)
    assert any("corroborator" in e for e in errs)
    assert any("retrieval_status" in e for e in errs)
    assert any("legal_status" in e for e in errs)


def test_validate_check_status_enum():
    rec = R.new_record(make_subject(), "E", "P", NOW)
    rec["checks_run"].append({"layer": "A", "provider": "opensanctions", "status": "done", "timestamp": NOW})
    assert any("status" in e for e in R.validate(rec))
