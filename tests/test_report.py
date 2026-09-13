from screening import lint as L
from screening import report as RP
from screening.case import Case
from screening.record import new_media_item
from screening.subjects import Subject
from tests.conftest import NOW


def build_case(cases_dir):
    c = Case.create(cases_dir, "GBC-BTN-2026-002", "Gelephu Mindfulness City Authority", NOW)
    c.add_subject(Subject(type="organization", name="Acme Pte Ltd", jurisdiction="SG"), NOW)
    c.add_subject(Subject(type="person", name="Mark Phillips", jurisdiction="AU"), NOW)
    return c


def test_render_has_sections_and_section_8_when_no_layer_c(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    for r in recs:
        r["checks_run"].append({"layer": "A", "provider": "opensanctions", "status": "ok", "timestamp": NOW,
                                "dataset": "default", "algorithm": "best", "threshold": 0.7, "limit": 10})
        r["checks_run"].append({"layer": "B", "provider": "web_search", "mode": "full", "mode_reason": "x",
                                "queries_run": ['"Mark Phillips"'], "languages": ["en"], "status": "ok", "timestamp": NOW})
    out = RP.render(c, recs, NOW)
    for h in ("Bottom line", "What was screened", "Limitations", "Findings", "Proposed additional subjects",
              "Coverage gaps", "Distribution note"):
        assert f"## {h}" in out
    assert L.SECTION_8_MARKER in out
    assert "GBC-BTN-2026-002" in out and "Gelephu Mindfulness City Authority" in out
    assert "No adverse media items meeting the identity-resolution criteria were returned" in out
    assert L.lint_report(out, recs) == []


def test_findings_cite_confirmed_only_and_possible_go_to_limitations(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    mp = recs[1]
    mp["checks_run"].append({"layer": "B", "provider": "web_search", "mode": "full", "mode_reason": "x",
                             "queries_run": [], "languages": ["en"], "status": "ok", "timestamp": NOW})
    mp["media_items"].append(new_media_item(
        title="Councillor fined", publisher="Example News", published="2024-06-01", url="https://news.example/1",
        retrieved=NOW, retrieval_status="full", identity="confirmed_subject", corroborator="role: councillor, Brisbane",
        legal_status="regulatory_action", source_type="wire", summary="A fine was imposed."))
    mp["media_items"].append(new_media_item(
        title="Different Mark Phillips arrested", publisher="Tabloid", published="2020-01-01", url="https://tab.example/2",
        retrieved=NOW, retrieval_status="snippet_only", identity="possible_subject", corroborator=None,
        legal_status="charged", source_type="low_accountability"))
    out = RP.render(c, recs, NOW)
    findings = L.report_section(out, "Findings")
    limits = L.report_section(out, "Limitations")
    assert "https://news.example/1" in findings and "regulatory_action" in findings
    assert "https://tab.example/2" not in findings and "https://tab.example/2" in limits
    assert L.lint_report(out, recs) == []


def test_layer_c_everywhere_drops_section_8_and_cites_scan_ids(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    for i, r in enumerate(recs):
        r["checks_run"].append({"layer": "C", "provider": "namescan", "tier": "sapphire", "status": "ok", "timestamp": NOW, "attempts": 1})
        r["layer_c"] = {"provider": "namescan", "tier": "sapphire", "scan_id": f"scan-{i}", "number_of_matches": i,
                        "adverse_media": "ok", "credits_consumed": 1.25, "reused_prior_scan": False, "matches": [],
                        "advanced_media_items": [], "test_mode": False, "scan_date": NOW}
    out = RP.render(c, recs, NOW)
    assert L.SECTION_8_MARKER not in out
    assert "scan-0" in out and "scan-1" in out


def test_watchlist_candidates_listed_as_unreviewed(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    recs[1]["watchlist_candidates"].append({"source": "opensanctions", "id": "Q1", "caption": "Mark Philips", "score": 0.74,
                                           "topics": ["role.pep"], "datasets": ["au_pep"], "entity": None,
                                           "assessment": "unreviewed", "proposed_disposition": "likely different person: different state"})
    out = RP.render(c, recs, NOW)
    assert "Q1" in out and "0.74" in out and "unreviewed" in out and "likely different person" in out


def test_sparse_layer_d_never_renders_none(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    org_rec = recs[0]
    org_rec["checks_run"].append({"layer": "D", "provider": "registries", "sources": ["gleif", "companies_house"],
                                  "status": "ok", "error": None, "timestamp": NOW})
    org_rec["layer_d"] = {
        "gleif": [{
            "lei": "X",
            "legal_name": "Acme",
            "status": None,
            "jurisdiction": None,
            "registered_as": None,
            "address": {"lines": [], "city": None, "country": None},
            "registration_status": None
        }],
        "companies_house": {
            "company_number": "1",
            "company_name": "Acme Ltd",
            "status": None,
            "incorporated": None,
            "type": None,
            "sic_codes": [],
            "accounts": {"next_due": None, "overdue": None},
            "registered_office": None,
            "officers": [{"name": "DOE, Jane", "role": None, "appointed_on": None, "resigned_on": None,
                          "nationality": None, "country_of_residence": None}]
        },
        "manual_findings": []
    }
    out = RP.render(c, recs, NOW)
    assert "None" not in out
    assert L.lint_report(out, recs) == []


def test_test_key_layer_c_keeps_section_8_and_is_declared(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    for i, r in enumerate(recs):
        r["checks_run"].append({"layer": "C", "provider": "namescan", "tier": "sapphire", "status": "ok",
                                "timestamp": NOW, "attempts": 1})
        r["layer_c"] = {"provider": "namescan", "tier": "sapphire", "scan_id": f"scan-{i}", "number_of_matches": 0,
                        "adverse_media": "not_requested", "credits_consumed": 0.0, "reused_prior_scan": False,
                        "matches": [], "advanced_media_items": [], "test_mode": True, "scan_date": NOW}
    out = RP.render(c, recs, NOW)
    assert L.SECTION_8_MARKER in out
    assert "Layer C was run with the NameScan TEST key for 2 subject(s): no real coverage, no credits spent." in out
    assert L.lint_report(out, recs) == []
