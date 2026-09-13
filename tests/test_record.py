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


def full_record():
    rec = R.new_record(make_subject(), "E", "P", NOW)
    rec["checks_run"] = [
        {"layer": "A", "provider": "opensanctions", "status": "ok", "timestamp": NOW},
        {"layer": "B", "provider": "web_search", "status": "ok", "timestamp": NOW},
        {"layer": "C", "provider": "namescan", "status": "ok", "timestamp": NOW},
        {"layer": "D", "provider": "registries", "status": "ok", "timestamp": NOW},
    ]
    rec["watchlist_candidates"] = [{"id": "Q1", "assessment": "unreviewed"}]
    rec["layer_c"] = {"scan_id": "ps-1"}
    rec["layer_d"] = {"gleif": [], "companies_house": None, "manual_findings": []}
    rec["proposed_subjects"] = [{"name": "Jane Doe", "type": "person"}]
    rec["coverage_gaps"] = ["Layer A (opensanctions) not run: no API key", "no DOB supplied",
                            "transliterated name — matcher precision reduced",
                            "Layer C (namescan) not run: no API key",
                            "Layer D Companies House not queried: no API key configured"]
    return rec


def test_replace_layer_a_drops_checks_candidates_and_its_gaps_only():
    rec = full_record()
    R.replace_layer(rec, "A")
    assert [c["layer"] for c in rec["checks_run"]] == ["B", "C", "D"]
    assert rec["watchlist_candidates"] == []
    assert "no DOB supplied" in rec["coverage_gaps"]
    assert "transliterated name — matcher precision reduced" in rec["coverage_gaps"]
    assert not any(g.startswith("Layer A") for g in rec["coverage_gaps"])
    assert rec["layer_c"] is not None and rec["layer_d"] is not None


def test_replace_layer_c_clears_payload_and_gaps():
    rec = full_record()
    R.replace_layer(rec, "C")
    assert [c["layer"] for c in rec["checks_run"]] == ["A", "B", "D"]
    assert rec["layer_c"] is None
    assert not any(g.startswith("Layer C") for g in rec["coverage_gaps"])
    assert rec["watchlist_candidates"] != []


def test_replace_layer_d_clears_payload_and_proposed_subjects():
    rec = full_record()
    R.replace_layer(rec, "D")
    assert [c["layer"] for c in rec["checks_run"]] == ["A", "B", "C"]
    assert rec["layer_d"] is None and rec["proposed_subjects"] == []
    assert not any(g.startswith("Layer D") for g in rec["coverage_gaps"])


def test_replace_layer_b_drops_only_layer_b():
    rec = full_record()
    rec["coverage_gaps"].append("Layer B (web_search) incomplete: no Dzongkha sources reachable")
    R.replace_layer(rec, "B")
    assert [c["layer"] for c in rec["checks_run"]] == ["A", "C", "D"]
    assert not any(g.startswith("Layer B") for g in rec["coverage_gaps"])


def test_replace_layer_d_keeps_manual_findings():
    rec = full_record()
    rec["layer_d"]["manual_findings"] = [{"registry": "ACRA", "url": "https://x", "retrieved": NOW,
                                          "fields": {"status": "Live"}, "note": None}]
    R.replace_layer(rec, "D")
    assert rec["layer_d"]["manual_findings"][0]["registry"] == "ACRA"
    assert rec["layer_d"]["gleif"] == [] and rec["layer_d"]["companies_house"] is None
    assert rec["proposed_subjects"] == []
