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


def test_layer_c_matches_and_vendor_media_are_rendered(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    for r in recs:
        r["checks_run"].append({"layer": "C", "provider": "namescan", "tier": "sapphire", "status": "ok",
                                "timestamp": NOW, "attempts": 1})
        r["layer_c"] = {"provider": "namescan", "tier": "sapphire", "scan_id": "ps-1", "number_of_matches": None,
                        "adverse_media": "ok", "credits_consumed": 1.25, "reused_prior_scan": False,
                        "test_mode": False, "scan_date": NOW, "match_rate_floor": 75,
                        "matches": [{"name": "Mark Phillips", "match_rate": 88, "category": "PEP",
                                     "matched_fields": "Name, DOB",
                                     "official_lists": [{"keyword": "Australian PEP", "is_current": True},
                                                        {"keyword": "EU Sanctions", "is_current": False}]}],
                        "advanced_media_items": [{"title": "Council fined over procurement",
                                                  "source_name": "Example News",
                                                  "published": "2024-06-01T00:00:00",
                                                  "link": "https://news.example/1"}]}
    out = RP.render(c, recs, NOW)
    findings = L.report_section(out, "Findings")
    assert ("Mark Phillips — match rate 88, category PEP, matched on Name, DOB; "
            "lists: Australian PEP (current) / EU Sanctions (not current) — assessment: unreviewed") in findings
    assert "Vendor adverse-media items (NameScan, not resolved to the subject by this system): 1" in findings
    assert "Council fined over procurement — Example News, 2024-06-01 https://news.example/1" in findings
    assert "unknown match(es)" in findings  # number_of_matches None never renders as None
    assert "None" not in out
    assert L.lint_report(out, recs) == []


def test_dispositioned_watchlist_candidate_renders_and_counts_in_bottom_line(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    recs[1]["watchlist_candidates"].append({"source": "opensanctions", "id": "Q1", "caption": "Mark Philips",
                                             "score": 0.74, "topics": ["role.pep"], "datasets": ["au_pep"],
                                             "entity": None, "assessment": "false_positive",
                                             "proposed_disposition": None, "dispositioned_by": "Jane Analyst",
                                             "dispositioned_at": "2026-09-14T09:00:00+00:00",
                                             "disposition_note": "different date of birth"})
    out = RP.render(c, recs, NOW)
    findings = L.report_section(out, "Findings")
    assert "dispositioned false_positive by Jane Analyst on 2026-09-14" in findings
    assert "different date of birth" in findings
    bottom = L.report_section(out, "Bottom line")
    assert "dispositioned by the commissioning party: 1" in bottom
    assert L.lint_report(out, recs) == []


def test_layer_c_match_disposition_renders(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    r = recs[1]
    r["checks_run"].append({"layer": "C", "provider": "namescan", "tier": "sapphire", "status": "ok",
                            "timestamp": NOW, "attempts": 1})
    r["layer_c"] = {"provider": "namescan", "tier": "sapphire", "scan_id": "ps-1", "number_of_matches": 1,
                    "adverse_media": "ok", "credits_consumed": 1.25, "reused_prior_scan": False,
                    "test_mode": False, "scan_date": NOW, "match_rate_floor": 75,
                    "matches": [{"name": "Mark Phillips", "match_rate": 88, "category": "PEP",
                                 "matched_fields": "Name", "official_lists": [],
                                 "assessment": "true_match", "dispositioned_by": "Jane Analyst",
                                 "dispositioned_at": "2026-09-14T09:00:00+00:00", "disposition_note": None}],
                    "advanced_media_items": []}
    out = RP.render(c, recs, NOW)
    findings = L.report_section(out, "Findings")
    assert "assessment: true_match — dispositioned true_match by Jane Analyst on 2026-09-14" in findings
    bottom = L.report_section(out, "Bottom line")
    assert "dispositioned by the commissioning party: 1" in bottom
    assert L.lint_report(out, recs) == []


def test_bottom_line_dispositioned_count_zero_when_none(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    out = RP.render(c, recs, NOW)
    assert "dispositioned by the commissioning party: 0" in L.report_section(out, "Bottom line")


def test_alias_scan_rendered_under_layer_c(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    r = recs[1]
    r["checks_run"].append({"layer": "C", "provider": "namescan", "tier": "sapphire", "status": "ok",
                            "timestamp": NOW, "attempts": 1})
    r["layer_c"] = {"provider": "namescan", "tier": "sapphire", "scan_id": "ps-1", "number_of_matches": 0,
                    "adverse_media": "ok", "credits_consumed": 1.25, "reused_prior_scan": False,
                    "test_mode": False, "scan_date": NOW, "match_rate_floor": 75, "matches": [],
                    "advanced_media_items": [],
                    "alias_scans": [{"alias": "M. Phillips", "scan_id": "ps-2", "number_of_matches": 1,
                                     "matches": [{"name": "M Phillips", "match_rate": 80, "category": "PEP",
                                                  "matched_fields": "Name", "official_lists": [],
                                                  "assessment": "unreviewed", "dispositioned_by": None,
                                                  "dispositioned_at": None, "disposition_note": None}],
                                     "adverse_media": "ok", "credits_consumed": 1.25,
                                     "reused_prior_scan": False, "test_mode": False}]}
    out = RP.render(c, recs, NOW)
    findings = L.report_section(out, "Findings")
    assert "Alias scan 'M. Phillips': scan ps-2, 1 match(es)" in findings
    assert "candidate: M Phillips — match rate 80" in findings
    assert L.lint_report(out, recs) == []


def test_vendor_titled_unresolved_item_under_limitations_does_not_trip_the_scan(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    mp = recs[1]
    mp["checks_run"].append({"layer": "B", "provider": "web_search", "mode": "full", "mode_reason": "x",
                             "queries_run": [], "languages": ["en"], "status": "ok", "timestamp": NOW})
    mp["media_items"].append(new_media_item(
        title="Clear Channel Holdings Ltd executive fined", publisher="Example News", published="2024-06-01",
        url="https://news.example/9", retrieved=NOW, retrieval_status="full", identity="possible_subject",
        corroborator=None, legal_status="regulatory_action", source_type="wire"))
    out = RP.render(c, recs, NOW)
    assert "Clear Channel Holdings Ltd executive fined" in L.report_section(out, "Limitations")
    assert not [v for v in L.lint_report(out, recs) if v.rule == "forbidden_phrase"]
